"""Release state machine: drives prepare/activate across both mirror repos.

Crash-safety model: the release intent is persisted before any repo call, and
every repo receipt is persisted locally as soon as it is observed. Before
issuing an operation the machine first asks the repo for an existing receipt
under the derived op key, so a control-service restart converges from
repo-side receipts without ever re-executing an operation.

Evidence-scoping model: a receipt only ever proves the single repository
operation whose key embeds the release identifier
(``rel:{release_id}:{repo}:{prepare|activate}``). Receipts are therefore never
shared across releases: byte-identical artifacts submitted under a different
release id must obtain their own prepare/activate receipts from both repos.
COMPLETED releases are additionally reconciled against live repo truth (the
receipt stored under each derived op key), so a completion whose evidence came
from another release is detected and re-converged.
"""
from __future__ import annotations

import base64
import logging
import threading

from app.common.httpjson import TransportError
from app.common.receipts import verify_receipt
from app.control import core

log = logging.getLogger("control.machine")


class Rejected(Exception):
    """Internal signal: the release has just been locked as REJECTED."""


class ReleaseMachine:
    def __init__(self, store, clients: dict, secrets: dict):
        self.store = store
        self.clients = clients  # repo name -> RepoClient
        self.secrets = secrets  # repo name -> HMAC secret

    def advance(self, release_id: str) -> None:
        rel = self.store.get_release(release_id)
        if rel is None or rel["state"] in core.TERMINAL_STATES:
            return
        try:
            self._run(rel)
        except Rejected:
            pass
        except Exception:  # noqa: BLE001 - keep the worker alive
            log.exception("advance failed for release %s", release_id)

    # ---- internals ----
    def _run(self, rel: dict) -> None:
        rid, sha = rel["release_id"], rel["sha256"]
        artifact_b64 = base64.b64encode(rel["artifact"]).decode()

        # Phase 1: prepare both repos with the identical candidate bytes.
        for repo in self.clients:
            receipt = self._ensure_op(rid, repo, core.OP_PREPARE, sha, artifact_b64)
            if receipt is None:
                self._set_state(rid, core.STATE_PREPARING)
                return
            self._check_digest(rid, repo, core.OP_PREPARE, sha, receipt)

        self._set_state(rid, core.STATE_ACTIVATING)

        # Phase 2: activate both repos; only identical digests may complete.
        for repo in self.clients:
            receipt = self._ensure_op(rid, repo, core.OP_ACTIVATE, sha, artifact_b64)
            if receipt is None:
                return
            self._check_digest(rid, repo, core.OP_ACTIVATE, sha, receipt)

        self._set_state(rid, core.STATE_COMPLETED)
        log.info("release %s completed (sha256=%s)", rid, sha)

    def _ensure_op(self, rid: str, repo: str, op: str, sha: str,
                   artifact_b64: str) -> dict | None:
        """Return the repo receipt for this op, persisting it locally.

        The receipt must be bound to THIS release's derived op key. Returns
        None when the repo is unreachable (the worker retries later). Raises
        Rejected when repo-side evidence conflicts with the release.
        """
        key = core.op_key(rid, repo, op)
        existing = self.store.get_receipt(rid, repo, op)
        if existing is not None:
            if self._bound_and_signed(repo, op, key, existing):
                return existing
            # Stale evidence (e.g. an old row borrowed from another release's
            # op key): it proves nothing about this release. Drop and re-derive
            # from the repo under the correct op key.
            log.warning("discarding %s receipt for release %s bound to foreign op_key %s",
                        op, rid, existing.get("op_key"))
            self.store.delete_receipt(rid, repo, op)
        client = self.clients[repo]
        try:
            # Post-crash adoption: the repo may already hold the first receipt.
            status, body = client.get_op(key)
            if status == 200:
                return self._adopt(rid, repo, op, key, body.get("receipt"))
            if status == 404:
                if op == core.OP_PREPARE:
                    status, body = client.prepare(key, sha, artifact_b64)
                else:
                    status, body = client.activate(key, sha)
                if status in (200, 201):
                    return self._adopt(rid, repo, op, key, body.get("receipt"))
                if status == 409:
                    self._reject_conflict(rid, repo, op, key, body)
                if status == 400 and (body.get("error") or {}).get("code") == "not_prepared":
                    # Repo lost its staging area; re-prepare, retry next tick.
                    client.prepare(core.op_key(rid, repo, core.OP_PREPARE), sha, artifact_b64)
                return None
            # 5xx or anything unexpected: treat as temporarily unreachable.
            log.warning("repo %s %s for %s -> HTTP %s", repo, op, rid, status)
            return None
        except TransportError as e:
            log.warning("repo %s unreachable for %s %s: %s", repo, op, rid, e)
            return None

    def _bound_and_signed(self, repo: str, op: str, key: str, receipt: dict) -> bool:
        """A receipt is usable only if it is signed AND bound to this exact op."""
        if not isinstance(receipt, dict) or not receipt:
            return False
        if receipt.get("op_key") != key:
            return False
        if receipt.get("repo") != repo or receipt.get("op") != op:
            return False
        return verify_receipt(self.secrets.get(repo, ""), receipt)

    def _adopt(self, rid: str, repo: str, op: str, key: str, receipt) -> dict | None:
        if not self._bound_and_signed(repo, op, key, receipt):
            self._reject(
                rid,
                f"镜像仓 {repo} 返回的 {op} 证据与本发布的派生操作键 {key} 不匹配"
                "（签名不符或操作键/仓/操作不一致），发布已锁定为拒绝",
            )
            raise Rejected()
        self.store.put_receipt(rid, repo, op, key, str(receipt.get("digest", "")), receipt)
        return receipt

    def _check_digest(self, rid: str, repo: str, op: str, sha: str, receipt: dict) -> None:
        if receipt.get("digest") != sha:
            self._reject(
                rid,
                f"镜像仓 {repo} 的 {op} 回执摘要不属于本发布"
                f"（收到 {receipt.get('digest')}，期望 {sha}），发布已锁定为拒绝",
            )
            raise Rejected()

    def reconcile_completed(self, release_id: str) -> None:
        """Re-verify a COMPLETED release against live repository truth.

        For each of the four derived op keys the repo must hold a signed
        receipt bound to that key and to the release digest. A missing (404)
        or foreign key means the completion was built on another release's
        evidence: the bogus local receipts are purged and the release is
        reopened so the normal path executes its own prepare/activate calls.
        Unreachable repos leave the state untouched for a later pass.
        """
        rel = self.store.get_release(release_id)
        if rel is None or rel["state"] != core.STATE_COMPLETED:
            return
        rid, sha = rel["release_id"], rel["sha256"]
        findings = []  # (repo, op, key, ok, repo_receipt)
        for repo in self.clients:
            for op in (core.OP_PREPARE, core.OP_ACTIVATE):
                key = core.op_key(rid, repo, op)
                try:
                    status, body = self.clients[repo].get_op(key)
                except TransportError as e:
                    log.info("reconcile %s: repo %s unreachable, keep state: %s", rid, repo, e)
                    return
                if status not in (200, 404):
                    log.info("reconcile %s: repo %s %s -> HTTP %s, keep state",
                             rid, repo, op, status)
                    return
                receipt = body.get("receipt") if status == 200 else None
                ok = (
                    status == 200
                    and self._bound_and_signed(repo, op, key, receipt)
                    and receipt.get("digest") == sha
                )
                findings.append((repo, op, key, ok, receipt))

        bad = [(repo, op, key) for repo, op, key, ok, _ in findings if not ok]
        for repo, op, key, ok, receipt in findings:
            if not ok:
                continue
            # Align the local copy with the repo's canonical first receipt.
            local = self.store.get_receipt(rid, repo, op)
            if local is None or local.get("receipt_id") != receipt.get("receipt_id"):
                self.store.put_receipt(rid, repo, op, key, sha, receipt)
        if bad:
            for repo, op, key in bad:
                log.warning("reconcile %s: repo %s has no valid evidence for %s; reopening",
                            rid, repo, key)
                self.store.delete_receipt(rid, repo, op)
            self.store.update_state(rid, core.STATE_PENDING)

    def _reject_conflict(self, rid: str, repo: str, op: str, key: str, body: dict) -> None:
        err = body.get("error") or {}
        existing = err.get("existing_digest", "<unknown>")
        # Best effort: keep the conflicting repo-side receipt as evidence.
        try:
            status, body2 = self.clients[repo].get_op(key)
            if status == 200:
                receipt = body2.get("receipt")
                if isinstance(receipt, dict) and receipt:
                    self.store.put_receipt(
                        rid, repo, op, key, str(receipt.get("digest", "")), receipt
                    )
        except TransportError:
            pass
        self._reject(rid, f"镜像仓 {repo} 拒绝了 {op}：操作键已绑定不同摘要（{existing}）")
        raise Rejected()

    def _reject(self, rid: str, message: str) -> None:
        log.error("release %s rejected: %s", rid, message)
        self._set_state(rid, core.STATE_REJECTED, error=message)

    def _set_state(self, rid: str, state: str, error: str | None = None) -> None:
        rel = self.store.get_release(rid)
        if rel is None or rel["state"] in core.TERMINAL_STATES:
            return  # terminal states are locked and never rewritten
        if rel["state"] != state or error:
            self.store.update_state(rid, state, error)


class Worker(threading.Thread):
    """Background reconciler: advances every non-terminal release.

    On process start it first reconciles every COMPLETED release against
    repository truth (healing completions borrowed from another release), then
    picks up all unfinished releases from the durable store; reconciliation is
    also repeated periodically. That is what makes a control-service restart
    converge.
    """

    def __init__(self, store, machine: ReleaseMachine, interval: float = 0.5,
                 reconcile_interval: float = 5.0):
        super().__init__(name="control-worker", daemon=True)
        self.store = store
        self.machine = machine
        self.interval = interval
        self.reconcile_ticks = max(1, int(round(reconcile_interval / interval)))
        self._stop_event = threading.Event()

    def stop(self) -> None:
        self._stop_event.set()

    def _reconcile_all(self) -> None:
        for rid in self.store.all_release_ids():
            if self._stop_event.is_set():
                return
            try:
                self.machine.reconcile_completed(rid)
            except Exception:  # noqa: BLE001 - never kill the worker
                log.exception("reconcile failed for release %s", rid)

    def run(self) -> None:
        tick = 0
        while not self._stop_event.is_set():
            try:
                if tick % self.reconcile_ticks == 0:
                    self._reconcile_all()
                for rid in self.store.pending_release_ids(core.TERMINAL_STATES):
                    if self._stop_event.is_set():
                        break
                    self.machine.advance(rid)
                tick += 1
            except Exception:  # noqa: BLE001 - never kill the worker
                log.exception("worker tick failed")
            self._stop_event.wait(self.interval)
