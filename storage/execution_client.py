"""SDK client for durable jobs; no public prompt/upload request is sent."""
from __future__ import annotations

import json
from pathlib import Path
from collections import OrderedDict
import threading
import time

from storage.delivery import receipt_file, volume_receipt

from storage.delivery_client import DeliveryClient
from storage.execution import (
    MEDIA_INPUT_EXTENSIONS, canonical, finalize_produced, inspect_execution, job_id, load_request,
    put_once, request_key, stage_request, validate_request,
)


# A live producer publishes ready immediately after produced. Give that
# normal publication window a bounded grace before a CPU repair reads media.
_READY_VISIBILITY_GRACE_SECONDS = 15


class DurableClient(DeliveryClient):
    def __init__(self, config, base):
        super().__init__(config, base)
        self._cache_lock = threading.RLock()
        self._requests = OrderedDict()
        self._request_bytes = 0
        self._calls = {}

    def remember_request(self, request):
        """Remember only validated immutable requests, bounded per SDK client."""
        cache = getattr(self, "_requests", None)
        if cache is None:
            return
        identifier = request["job_id"]
        validate_request(request, self.config, identifier)
        raw = canonical(request)
        with self._cache_lock:
            previous = cache.pop(identifier, None)
            if previous is not None:
                self._request_bytes -= len(previous)
            cache[identifier] = raw
            self._request_bytes += len(raw)
            while len(cache) > 64 or self._request_bytes > 16 * 1024 * 1024:
                _, evicted = cache.popitem(last=False)
                self._request_bytes -= len(evicted)

    def _request(self, identifier):
        cache = getattr(self, "_requests", None)
        if cache is not None:
            with self._cache_lock:
                if identifier in cache:
                    cache.move_to_end(identifier)
                    return json.loads(cache[identifier])
        request = load_request(self.volume, self.config, identifier)
        self.remember_request(request)
        return request

    def _call(self, call_id):
        cache = getattr(self, "_calls", None)
        if cache is None:
            return self.modal.FunctionCall.from_id(call_id)
        with self._cache_lock:
            if call_id not in cache:
                cache[call_id] = self.modal.FunctionCall.from_id(call_id)
            return cache[call_id]

    def _forget_call(self, call_id):
        cache = getattr(self, "_calls", None)
        if cache is not None:
            with self._cache_lock:
                cache.pop(call_id, None)

    def preflight(self):
        info = super().preflight()
        if info.get("execution_schema") != 1:
            raise RuntimeError("Deployment does not support durable execution v1")
        return info

    def stage(self, identifier, intent, graph, settings, output_nodes=None,
              expected_inputs=None, *, output_inputs=None):
        # Old v1 image-only deployments cannot reconstruct media refs. Check
        # the CPU identity probe before publishing any new video/audio input.
        media = any(isinstance(value, str) and value.startswith("@") and
                    Path(value[1:]).suffix.lower() in MEDIA_INPUT_EXTENSIONS
                    for value in settings.values())
        if media or output_inputs:
            info = self.preflight()
            if media and info.get("execution_media_inputs") != 1:
                raise RuntimeError("Deployment does not support durable video/audio inputs; redeploy first")
            if output_inputs and info.get("execution_cloud_inputs") != 1:
                raise RuntimeError("Deployment does not support direct cloud outputs; redeploy first")
        request = stage_request(self.volume, self.config, identifier, intent, graph,
                                settings, output_nodes, expected_inputs=expected_inputs,
                                output_inputs=output_inputs)
        self.remember_request(request)
        return request

    def status(self, identifier):
        request = self._request(identifier)
        state = inspect_execution(self.volume, request)
        if state["status"] == "produced":
            ready = volume_receipt(self.volume, self.config, state["prompt_id"])
            deadline = time.monotonic() + _READY_VISIBILITY_GRACE_SECONDS
            while ready is None and time.monotonic() < deadline:
                time.sleep(min(1, max(0, deadline - time.monotonic())))
                ready = volume_receipt(self.volume, self.config, state["prompt_id"])
            if ready is None:
                # A dead producer may never publish ready. Keep the original
                # verified repair path; absence is not proof of completion.
                return finalize_produced(self.volume, request, state)
            if ready != state["receipt"] or not state["remote_files"]:
                raise RuntimeError("Execution receipt missing or inconsistent")
            for item in state["remote_files"]:
                receipt_file(ready, item)
            return {**state, "status": "completed", "receipt": ready}
        return state

    def prepare_retry(self, identifier, attempt, allow_unknown=False):
        """Explicitly create a new attempt from persisted inputs, without dispatch."""
        from storage.delivery import token
        token(attempt, "retry attempt")
        original = load_request(self.volume, self.config, identifier)
        state = self.status(identifier)
        if state["status"] not in ("failed", "admitted", "submitted"):
            raise RuntimeError("Only failed or unresolved admitted jobs may be retried")
        if state["status"] != "failed" and not allow_unknown:
            raise RuntimeError("Prior execution may still finish; explicit duplicate-risk acknowledgement required")
        replacement = job_id(["retry", identifier, attempt], original["intent"])
        request = {**original, "job_id": replacement}
        validate_request(request, self.config, replacement)
        put_once(self.volume, request_key(self.config, replacement), request)
        return {"status": "prepared", "job_id": replacement, "previous_job_id": identifier}

    def start(self, identifier, *, reconcile=False):
        """Dispatch unclaimed work; observe a claimed job unless explicitly asked to reconcile."""
        state = self.status(identifier)
        if state["status"] in ("completed", "failed"):
            return state
        if state["status"] in ("admitted", "submitted") and not reconcile:
            return {**state, "status": "observing", "ledger_status": state["status"],
                    "previous_call_id": None, "observer_call_status": "not_attached",
                    "error": "Existing claim; observing without GPU dispatch"}
        if state["status"] not in ("prepared", "admitted", "submitted"):
            raise RuntimeError("Unexpected durable ledger state; refusing dispatch")
        self.preflight()
        call = self._model().execute_durable.spawn(
            identifier, self.config["instance"], self.config["version"])
        calls = getattr(self, "_calls", None)
        if calls is not None:
            with self._cache_lock:
                calls[call.object_id] = call
        return {"status": "dispatched", "call_id": call.object_id,
                "job_id": identifier}

    def poll(self, identifier, call_id=None):
        """Observe a call, or only the durable ledger when no call is supplied.

        A finished observer call is not proof that the owning GPU stopped.
        Only immutable terminal evidence can resolve a claimed job.
        """
        if call_id is None:
            state = self.status(identifier)
            if state["status"] in ("completed", "failed"):
                return state
            if state["status"] not in ("admitted", "submitted"):
                raise RuntimeError("Observed durable claim is missing; refusing replay")
            return None
        from modal.exception import TimeoutError as ModalTimeoutError
        error = None
        try:
            result = self._call(call_id).get(timeout=0)
        except (TimeoutError, ModalTimeoutError) as exc:
            # SDK 1.6.4 poll_function raises builtins.TimeoutError while pending.
            # FunctionTimeoutError / OutputExpiredError are terminal subclasses.
            if type(exc) in (TimeoutError, ModalTimeoutError):
                return None
            result = None
            error = f"{type(exc).__name__}: {exc}"
        except Exception as exc:
            # A call can fail after durable output publication. Check evidence
            # before reporting an uncertain attempt to the operator.
            result = None
            error = f"{type(exc).__name__}: {exc}"
        self._forget_call(call_id)
        if result is not None and (not isinstance(result, dict) or
                                   result.get("job_id") != identifier):
            raise RuntimeError("Durable call result identity mismatch")
        # Success or failure is accepted from authoritative Volume records.
        state = self.status(identifier)
        if state["status"] in ("completed", "failed"):
            return state
        reason = error or (result or {}).get("error") or "No durable terminal evidence"
        if state["status"] in ("admitted", "submitted"):
            # This invocation is finished, but another owner may still publish.
            # Switch to CPU ledger observation rather than polling this call again.
            return {**state, "status": "observing", "ledger_status": state["status"],
                    "previous_call_id": call_id,
                    "observer_call_status": (result or {}).get("status", "unavailable"),
                    "error": reason}
        return {**state, "status": "uncertain", "previous_call_id": call_id,
                "error": "Modal call result unavailable before claim; reconcile the same job. " + reason}
