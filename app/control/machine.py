"""Release state machine: drives prepare/activate across both mirror repos.

Crash-safety model: the release intent is persisted before any repo call, and
every repo receipt is persisted locally as soon as it is observed. Before
issuing an operation the machine first asks the repo for an existing receipt
under the derived op key, so a control-service restart converges from
repo-side receipts without ever re-executing an operation.

Evidence scoping: a receipt only ever certifies ONE repository-side op key,
and that key is derived from the release identifier
(``rel:{release_id}:{repo}:{prepare|activate}``). A receipt collected for
another release id — even when its digest is identical — is therefore not
evidence for this release and is never reused. Every release must obtain its
own prepare/activate receipts under its own keys from both repos.
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
        """Drive one release forward, re-verifying its persisted evidence.

        Runs on every worker tick, so it doubles as crash recovery: a release
        that was wrongly marked COMPLETED on evidence belonging to another
        release id is reopened and reconverged from real repository state.
        """
        rel = self.store.get_release(release_id)
        if rel is None or rel["state"] == core.STATE_REJECTED:
            return
        try:
            self._review_locked(rel)
            rel = self.store.get_release(release_id)
            if rel is None or rel["state"] in core.TERMINAL_STATES:
                return
            self._run(rel)
        except Rejected:
            pass
        except Exception:  # noqa: BLE001 - keep the worker alive
            log.exception("advance failed for release %s", release_id)

    def review(self, release_id: str) -> None:
        """Network-free evidence check for read paths (queries / replays).

        Drops receipts bound to a different release-derived op key and
        reopens a falsely COMPLETED release; the background worker then
        reconverges it against real repository state. Never blocks on the
        network and never unlocks a REJECTED release.
        """
        rel = self.store.get_release(release_id)
        if rel is None or rel["state"] == core.STATE_REJECTED:
            return
        try:
            self._review_locked(rel)
        except Rejected:
            pass
        except Exception:  # noqa: BLE001 - read paths must stay available
            log.exception("review failed for release %s", release_id)

    # ---- internals ----
    def _slots(self, rid: str):
        for repo in self.clients:
            for op in (core.OP_PREPARE, core.OP_ACTIVATE):
                yield repo, op, core.op_key(rid, repo, op)

    def _review_locked(self, rel: dict) -> None:
        """Validate every persisted receipt against its derived op key.

        Receipts carrying another release's op_key are discarded (legacy
        cross-release poisoning); own-key receipts with a bad signature or a
        foreign digest are genuine repo-side conflicts and lock REJECTED.
        A COMPLETED release without four valid own-key receipts is reopened.
        """
        rid, sha = rel["release_id"], rel["sha256"]
        valid = 0
        all_prepares_valid = True
        foreign_evidence = False
        for repo, op, key in self._slots(rid):
            receipt = self.store.get_receipt(rid, repo, op)
            if receipt is None:
                if op == core.OP_PREPARE:
                    all_prepares_valid = False
                continue
            kind = self._classify(repo, op, key, sha, receipt)
            if kind == "valid":
                valid += 1
                continue
            if kind == "foreign-key":
                # Genuine receipt, but for a different release's op key:
                # it proves nothing about this release.
                log.warning(
                    "release %s: dropping %s/%s receipt bound to foreign op_key %s",
                    rid, repo, op, receipt.get("op_key"),
                )
                self.store.delete_receipt(rid, repo, op)
                foreign_evidence = True
                if op == core.OP_PREPARE:
                    all_prepares_valid = False
                continue
            # Own-key evidence that is forged or carries another digest:
            # keep it as evidence and lock the release.
            self.store.put_receipt(
                rid, repo, op, key, str(receipt.get("digest", "")), receipt
            )
            if kind == "bad-signature":
                self._reject(
                    rid, f"镜像仓 {repo} 的 {op} 证据签名校验失败，发布已锁定为拒绝"
                )
            else:
                self._reject(
                    rid,
                    f"镜像仓 {repo} 的 {op} 回执摘要不属于本发布"
                    f"（收到 {receipt.get('digest')}，期望 {sha}），发布已锁定为拒绝",
                )
            raise Rejected()

        if rel["state"] == core.STATE_COMPLETED and (foreign_evidence or valid < 4):
            state = core.STATE_ACTIVATING if all_prepares_valid else core.STATE_PREPARING
            self._reopen(rid, state)

    def _classify(self, repo: str, op: str, key: str, sha: str, receipt) -> str:
        """Return one of: valid | foreign-key | foreign-digest | bad-signature."""
        if not isinstance(receipt, dict) or not receipt:
            return "foreign-key"
        if receipt.get("op_key") != key or receipt.get("repo") != repo \
                or receipt.get("op") != op:
            return "foreign-key"
        if not verify_receipt(self.secrets.get(repo, ""), receipt):
            return "bad-signature"
        if receipt.get("digest") != sha:
            return "foreign-digest"
        return "valid"

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

        Only a receipt under the release-derived op key itself is accepted;
        receipts collected for other release ids (even with an identical
        digest) are never reused. Returns None when the repo is unreachable
        (the worker retries later); raises Rejected on conflicting evidence.
        """
        key = core.op_key(rid, repo, op)
        existing = self.store.get_receipt(rid, repo, op)
        if existing is not None:
            kind = self._classify(repo, op, key, sha, existing)
            if kind == "valid":
                return existing
            if kind == "foreign-key":
                self.store.delete_receipt(rid, repo, op)
            else:
                self._reject_inline(rid, repo, op, sha, existing, kind)

        client = self.clients[repo]
        try:
            # Post-crash adoption: the repo may already hold the first receipt.
            status, body = client.get_op(key)
            if status == 200:
                return self._adopt(rid, repo, op, key, sha, body.get("receipt"))
            if status == 404:
                if op == core.OP_PREPARE:
                    status, body = client.prepare(key, sha, artifact_b64)
                else:
                    status, body = client.activate(key, sha)
                if status in (200, 201):
                    return self._adopt(rid, repo, op, key, sha, body.get("receipt"))
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

    def _adopt(self, rid: str, repo: str, op: str, key: str, sha: str,
               receipt) -> dict | None:
        if not isinstance(receipt, dict) or not receipt:
            log.warning("repo %s returned an empty receipt for %s", repo, key)
            return None
        kind = self._classify(repo, op, key, sha, receipt)
        if kind == "foreign-key":
            log.warning("repo %s answered %s with receipt bound to %s",
                        repo, key, receipt.get("op_key"))
            self._reject(
                rid,
                f"镜像仓 {repo} 的 {op} 证据与本发布派生操作键不符，发布已锁定为拒绝",
            )
            raise Rejected()
        if kind != "valid":
            self._reject_inline(rid, repo, op, sha, receipt, kind)
        self.store.put_receipt(rid, repo, op, key, sha, receipt)
        return receipt

    def _reject_inline(self, rid: str, repo: str, op: str, sha: str,
                       receipt: dict, kind: str) -> None:
        self.store.put_receipt(
            rid, repo, op, core.op_key(rid, repo, op),
            str(receipt.get("digest", "")), receipt,
        )
        if kind == "bad-signature":
            self._reject(rid, f"镜像仓 {repo} 的 {op} 证据签名校验失败，发布已锁定为拒绝")
        else:
            self._reject(
                rid,
                f"镜像仓 {repo} 的 {op} 回执摘要不属于本发布"
                f"（收到 {receipt.get('digest')}，期望 {sha}），发布已锁定为拒绝",
            )
        raise Rejected()

    def _check_digest(self, rid: str, repo: str, op: str, sha: str, receipt: dict) -> None:
        if receipt.get("digest") != sha:
            self._reject(
                rid,
                f"镜像仓 {repo} 的 {op} 回执摘要不属于本发布"
                f"（收到 {receipt.get('digest')}，期望 {sha}），发布已锁定为拒绝",
            )
            raise Rejected()

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

    def _reopen(self, rid: str, state: str) -> None:
        """Leave a false COMPLETED state (REJECTED stays locked forever)."""
        rel = self.store.get_release(rid)
        if rel is None or rel["state"] != core.STATE_COMPLETED:
            return
        log.error("release %s: reopening false COMPLETED -> %s; persisted evidence"
                  " did not match its release-derived op keys", rid, state)
        self.store.update_state(rid, state)

    def _set_state(self, rid: str, state: str, error: str | None = None) -> None:
        rel = self.store.get_release(rid)
        if rel is None or rel["state"] == core.STATE_REJECTED:
            return  # rejection is locked and never rewritten
        if rel["state"] == core.STATE_COMPLETED and state != core.STATE_COMPLETED:
            return  # completion only changes via the explicit _reopen path
        if rel["state"] != state or error:
            self.store.update_state(rid, state, error)


class Worker(threading.Thread):
    """Background reconciler: advances every release that is not REJECTED.

    COMPLETED releases are re-checked too via a cheap local evidence check,
    so a falsely COMPLETED release (e.g. legacy cross-release receipts) is
    reopened and reconverged right after a process start. On start the worker
    picks up all unfinished releases from the durable store, which is what
    makes a control-service restart converge.
    """

    def __init__(self, store, machine: ReleaseMachine, interval: float = 0.5):
        super().__init__(name="control-worker", daemon=True)
        self.store = store
        self.machine = machine
        self.interval = interval
        self._stop_event = threading.Event()

    def stop(self) -> None:
        self._stop_event.set()

    def run(self) -> None:
        while not self._stop_event.is_set():
            try:
                for rid in self.store.release_ids_excluding((core.STATE_REJECTED,)):
                    if self._stop_event.is_set():
                        break
                    self.machine.advance(rid)
            except Exception:  # noqa: BLE001 - never kill the worker
                log.exception("worker tick failed")
            self._stop_event.wait(self.interval)
