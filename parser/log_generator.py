    # =========================================================
    # log_generator.py — Synthetic log generator for testing
    # Generates correlated step_N_logs.json files that match
    # the LogEntry/NetworkEvent schemas from models.py.
    # Standalone utility — not part of the analysis pipeline.
    # =========================================================

    import json
    import os
    import random
    from datetime import datetime, timedelta

    from models import load_project_data, extract_step_context


    # =========================================================
    # ENDPOINT MAPPING — maps UI elements to realistic API calls
    # =========================================================
    ELEMENT_TO_ENDPOINT = {
        # App launch / verification
        "waitUntil": ("GET", "/api/v1/app/health"),
        "abstractVerification": ("GET", "/api/v1/session/state"),
        # Navigation / modal
        "Carpi": ("POST", "/api/v1/ui/dismiss-modal"),
        "Uye olmadan devam et": ("POST", "/api/v1/auth/guest-session"),
        "Izin Ver": ("POST", "/api/v1/permissions/notification"),
        # Flight search flow
        "Nereden": ("GET", "/api/v1/airports/search?type=departure"),
        "Izmir": ("POST", "/api/v1/flights/set-departure", {"city": "Izmir", "code": "ADB"}),
        "Nereye": ("GET", "/api/v1/airports/search?type=arrival"),
        "Ankara": ("POST", "/api/v1/flights/set-arrival", {"city": "Ankara", "code": "ESB"}),
        "Gidis Tarihi": ("GET", "/api/v1/calendar/available-dates"),
        "21": ("POST", "/api/v1/flights/set-date", {"day": 21}),
        "UCUZ BILET BUL": ("POST", "/api/v1/flights/search"),
        # Results / sorting
        "Kalkis Saati": ("POST", "/api/v1/flights/sort", {"sort_by": "departure_time"}),
        "sonuclari goster": ("POST", "/api/v1/flights/filter/apply"),
        # Flight selection
        "12.15->13.35": ("POST", "/api/v1/flights/select", {"flight_id": "TK2124", "dep": "12:15", "arr": "13:35"}),
        "BASIC": ("POST", "/api/v1/bookings/select-package", {"package": "BASIC"}),
    }

    # Normalized key lookup: strip Turkish chars for matching
    def _normalize(text: str) -> str:
        replacements = {
            "ç": "c", "Ç": "C", "ğ": "g", "Ğ": "G", "ı": "i", "İ": "I",
            "ö": "o", "Ö": "O", "ş": "s", "Ş": "S", "ü": "u", "Ü": "U",
        }
        for k, v in replacements.items():
            text = text.replace(k, v)
        return text


    def _find_endpoint(element: str, action: str):
        """Find the best matching endpoint for a step."""
        norm_element = _normalize(element)
        # Direct match
        if norm_element in ELEMENT_TO_ENDPOINT:
            return ELEMENT_TO_ENDPOINT[norm_element]
        # Partial match
        for key, val in ELEMENT_TO_ENDPOINT.items():
            if key.lower() in norm_element.lower() or norm_element.lower() in key.lower():
                return val
        # Action-based fallback
        if action in ELEMENT_TO_ENDPOINT:
            return ELEMENT_TO_ENDPOINT[action]
        return ("GET", f"/api/v1/ui/action/{norm_element.lower().replace(' ', '-')}")


    # =========================================================
    # SCENARIO DEFINITIONS
    # =========================================================
    SCENARIOS = {
        "NORMAL": {
            "description": "All steps succeed with healthy responses",
            "failure_steps": {},
        },
        "NETWORK_FAILURE": {
            "description": "A specific step encounters a server error",
            "failure_steps": {},  # Set dynamically via fail_at_step
        },
        "TIMEOUT": {
            "description": "A specific step experiences a request timeout",
            "failure_steps": {},
        },
        "AUTH_FAILURE": {
            "description": "A specific step gets an authentication error",
            "failure_steps": {},
        },
        "CASCADING": {
            "description": "A step fails and subsequent steps show related downstream errors",
            "failure_steps": {},
        },
    }


    def _generate_timestamp(base_time: datetime, step_offset_s: float, intra_offset_ms: float) -> str:
        """Generate an ISO timestamp with millisecond precision."""
        ts = base_time + timedelta(seconds=step_offset_s, milliseconds=intra_offset_ms)
        return ts.strftime("%Y-%m-%dT%H:%M:%S.") + f"{ts.microsecond // 1000:03d}Z"


    def generate_step_logs(
        ctx: dict,
        step_idx: int,
        base_time: datetime,
        step_offset_s: float,
        failure_type: str = None,
        cascade_source: int = None,
    ) -> list:
        """Generate a list of log entries for a single step.

        Args:
            ctx: Step context from extract_step_context.
            step_idx: The step index.
            base_time: Base timestamp for the test run.
            step_offset_s: Seconds offset from base_time for this step.
            failure_type: None, "server_error", "timeout", "auth_error", or "cascade".
            cascade_source: If failure_type is "cascade", which step caused it.

        Returns:
            List of log entry dicts matching the step_N_logs.json format.
        """
        logs = []
        action = ctx.get("action", "unknown")
        target = ctx.get("target", "unknown")
        instruction = ctx.get("instruction", "unknown")

        # Skip wait steps — minimal log
        if action == "wait":
            logs.append({
                "timestamp": _generate_timestamp(base_time, step_offset_s, 0),
                "level": "info",
                "source": "application",
                "message": f"Step {step_idx}: Waiting (no action required).",
            })
            return logs

        # --- Application log: user action ---
        action_verb = {
            "click": "tapped",
            "waitUntil": "waiting for",
            "abstractVerification": "verifying",
        }.get(action, "performed action on")

        logs.append({
            "timestamp": _generate_timestamp(base_time, step_offset_s, 0),
            "level": "info",
            "source": "application",
            "message": f"Step {step_idx}: User {action_verb} element '{target}'.",
        })

        # --- Network request ---
        endpoint_info = _find_endpoint(target, action)
        method = endpoint_info[0]
        url = endpoint_info[1]
        body = endpoint_info[2] if len(endpoint_info) > 2 else None

        req_offset = random.uniform(50, 150)
        req_msg = f"{method} {url} - Request sent"
        if body:
            req_msg += f" with payload: {json.dumps(body)}"

        logs.append({
            "timestamp": _generate_timestamp(base_time, step_offset_s, req_offset),
            "level": "info",
            "source": "network",
            "message": req_msg,
        })

        # --- Network response (varies by failure_type) ---
        if failure_type == "server_error":
            latency = random.uniform(200, 800)
            status = random.choice([500, 502, 503])
            error_msg = {500: "Internal Server Error", 502: "Bad Gateway", 503: "Service Unavailable"}[status]
            logs.append({
                "timestamp": _generate_timestamp(base_time, step_offset_s, req_offset + latency),
                "level": "error",
                "source": "network",
                "message": f"{method} {url} - {status} {error_msg} ({latency:.0f}ms)",
            })
            logs.append({
                "timestamp": _generate_timestamp(base_time, step_offset_s, req_offset + latency + 10),
                "level": "error",
                "source": "application",
                "message": f"Step {step_idx}: Action failed — server returned {status}. Element '{target}' state may be inconsistent.",
            })

        elif failure_type == "timeout":
            latency = random.uniform(15000, 30000)
            logs.append({
                "timestamp": _generate_timestamp(base_time, step_offset_s, req_offset + latency),
                "level": "error",
                "source": "network",
                "message": f"{method} {url} - Request timed out after {latency:.0f}ms",
            })
            logs.append({
                "timestamp": _generate_timestamp(base_time, step_offset_s, req_offset + latency + 10),
                "level": "error",
                "source": "application",
                "message": f"Step {step_idx}: Network timeout while processing '{target}'. Retries exhausted.",
            })

        elif failure_type == "auth_error":
            latency = random.uniform(100, 300)
            status = random.choice([401, 403])
            error_msg = {401: "Unauthorized", 403: "Forbidden"}[status]
            logs.append({
                "timestamp": _generate_timestamp(base_time, step_offset_s, req_offset + latency),
                "level": "error",
                "source": "network",
                "message": f"{method} {url} - {status} {error_msg} ({latency:.0f}ms)",
            })
            logs.append({
                "timestamp": _generate_timestamp(base_time, step_offset_s, req_offset + latency + 10),
                "level": "error",
                "source": "system",
                "message": f"Step {step_idx}: Authentication/authorization failed for action on '{target}'. Session may have expired.",
            })

        elif failure_type == "cascade":
            latency = random.uniform(300, 1500)
            status = random.choice([400, 409, 422])
            error_msg = {400: "Bad Request", 409: "Conflict", 422: "Unprocessable Entity"}[status]
            logs.append({
                "timestamp": _generate_timestamp(base_time, step_offset_s, req_offset + latency),
                "level": "error",
                "source": "network",
                "message": f"{method} {url} - {status} {error_msg} ({latency:.0f}ms): Dependent resource from step {cascade_source} is in an invalid state.",
            })
            logs.append({
                "timestamp": _generate_timestamp(base_time, step_offset_s, req_offset + latency + 10),
                "level": "warning",
                "source": "application",
                "message": f"Step {step_idx}: Action on '{target}' failed due to upstream dependency (step {cascade_source}). Data may be stale or missing.",
            })

        else:
            # Normal success
            latency = random.uniform(80, 500)
            logs.append({
                "timestamp": _generate_timestamp(base_time, step_offset_s, req_offset + latency),
                "level": "info",
                "source": "network",
                "message": f"{method} {url} - 200 OK ({latency:.0f}ms)",
            })

        # --- Application log: result ---
        if failure_type is None:
            logs.append({
                "timestamp": _generate_timestamp(base_time, step_offset_s, req_offset + (latency if failure_type is None else 0) + 50),
                "level": "info",
                "source": "application",
                "message": f"Step {step_idx}: Action completed successfully for '{target}'.",
            })

        return logs


    def generate_logs_for_scenario(
        steps_path: str,
        output_path: str = None,
        scenario: str = "NORMAL",
        fail_at_step: int = None,
        cascade_depth: int = 3,
        seed: int = 42,
    ):
        """Generate step_N_logs.json files for all steps under a given scenario.

        Args:
            steps_path: Path to directory containing steps.json.
            output_path: Where to write log files (defaults to steps_path).
            scenario: One of NORMAL, NETWORK_FAILURE, TIMEOUT, AUTH_FAILURE, CASCADING.
            fail_at_step: Which step index to inject the failure at (required for non-NORMAL).
            cascade_depth: How many steps after fail_at_step to cascade (CASCADING only).
            seed: Random seed for reproducibility.
        """
        random.seed(seed)
        if output_path is None:
            output_path = steps_path

        steps_data = load_project_data(steps_path)
        if not steps_data:
            print("ERROR: Could not load steps.json")
            return

        base_time = datetime(2025, 4, 6, 10, 0, 0)
        failure_type_map = {
            "NETWORK_FAILURE": "server_error",
            "TIMEOUT": "timeout",
            "AUTH_FAILURE": "auth_error",
            "CASCADING": "server_error",  # root cause is server_error, downstream is cascade
        }

        print(f"Generating logs: scenario={scenario}, fail_at_step={fail_at_step}, steps={len(steps_data)}")

        for i, raw_step in enumerate(steps_data):
            ctx = extract_step_context(raw_step, i)
            step_idx = ctx["step_num"]
            step_offset = i * random.uniform(1.5, 4.0)

            # Determine failure type for this step
            ft = None
            cascade_src = None
            if scenario != "NORMAL" and fail_at_step is not None:
                if i == fail_at_step:
                    ft = failure_type_map.get(scenario)
                elif scenario == "CASCADING" and fail_at_step < i <= fail_at_step + cascade_depth:
                    ft = "cascade"
                    cascade_src = fail_at_step

            logs = generate_step_logs(ctx, step_idx, base_time, step_offset, ft, cascade_src)

            log_file = os.path.join(output_path, f"step_{step_idx}_logs.json")
            with open(log_file, "w", encoding="utf-8") as f:
                json.dump(logs, f, indent=2, ensure_ascii=False)

        print(f"Generated {len(steps_data)} log files in {output_path}")


    # =========================================================
    # CLI ENTRY POINT
    # =========================================================
    if __name__ == "__main__":
        import argparse

        parser = argparse.ArgumentParser(description="Generate synthetic test logs")
        parser.add_argument("--steps-path", default="./steps", help="Path to steps directory")
        parser.add_argument("--output-path", default=None, help="Output directory (default: same as steps-path)")
        parser.add_argument("--scenario", default="NORMAL",
                            choices=["NORMAL", "NETWORK_FAILURE", "TIMEOUT", "AUTH_FAILURE", "CASCADING"])
        parser.add_argument("--fail-at-step", type=int, default=None,
                            help="Step index to inject failure (required for non-NORMAL scenarios)")
        parser.add_argument("--cascade-depth", type=int, default=3,
                            help="Number of steps to cascade after failure (CASCADING only)")
        parser.add_argument("--seed", type=int, default=42, help="Random seed")

        args = parser.parse_args()

        if args.scenario != "NORMAL" and args.fail_at_step is None:
            parser.error(f"--fail-at-step is required for scenario {args.scenario}")

        generate_logs_for_scenario(
            steps_path=args.steps_path,
            output_path=args.output_path,
            scenario=args.scenario,
            fail_at_step=args.fail_at_step,
            cascade_depth=args.cascade_depth,
            seed=args.seed,
        )
