"""SDK client for durable jobs; no public prompt/upload request is sent."""
from __future__ import annotations

from pathlib import Path

from storage.delivery_client import DeliveryClient
from storage.execution import (
    MEDIA_INPUT_EXTENSIONS, finalize_produced, inspect_execution, job_id, load_request,
    put_once, request_key, stage_request, validate_request,
)


class DurableClient(DeliveryClient):
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
        return stage_request(self.volume, self.config, identifier, intent, graph,
                             settings, output_nodes, expected_inputs=expected_inputs,
                             output_inputs=output_inputs)

    def status(self, identifier):
        request = load_request(self.volume, self.config, identifier)
        state = inspect_execution(self.volume, request)
        if state["status"] == "produced":
            return finalize_produced(self.volume, request, state)
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
            result = self.modal.FunctionCall.from_id(call_id).get(timeout=0)
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
