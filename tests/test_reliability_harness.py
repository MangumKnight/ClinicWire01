#!/usr/bin/env python3
"""
Core Reliability Test Harness for ClinicWire
Tests: Long-hold/voicemail, concurrency, outcome fidelity, guardrails
Run with SIMULATE=true
"""

import asyncio
import os
import sys
import json
import time
from datetime import datetime, timezone, timedelta
from typing import List, Dict, Any
import httpx
import uuid

# Configuration
BASE_URL = os.getenv("TEST_BASE_URL", "http://127.0.0.1:8001")
SIMULATE = os.getenv("SIMULATE", "true")

# Test results collector
test_results = {
    "A_long_hold": {"pass": 0, "fail": 0, "details": []},
    "B_concurrency": {"pass": 0, "fail": 0, "details": []},
    "C_outcomes": {"pass": 0, "fail": 0, "details": []},
    "D_guardrails": {"pass": 0, "fail": 0, "details": []}
}


class TestHarness:
    def __init__(self):
        self.client = httpx.AsyncClient(base_url=BASE_URL, timeout=30.0)
        
    async def __aenter__(self):
        return self
        
    async def __aexit__(self, *args):
        await self.client.aclose()

    async def create_task(self, patient_suffix: str = "", scenario: str = "normal") -> Dict[str, Any]:
        """Create a test task"""
        data = {
            "workflow_type": "POC_SIGNATURE",
            "patient_alias": f"Test Patient {patient_suffix}",
            "doctor_name": f"Dr. Test {scenario}",
            "doctor_phone": "+19195551234",
            "therapist_phone": "+19195555678",
            "notes": f"Test scenario: {scenario}"
        }
        
        resp = await self.client.post("/tasks", json=data)
        return resp.json()

    async def get_task(self, task_id: str) -> Dict[str, Any]:
        """Get task details"""
        resp = await self.client.get(f"/tasks/{task_id}")
        return resp.json()

    async def trigger_call(self, task_id: str) -> Dict[str, Any]:
        """Manually trigger a call"""
        resp = await self.client.post(f"/tasks/{task_id}/call")
        return resp.json()

    async def simulate_webhook(self, call_sid: str, status: str, duration: str = "0") -> Dict[str, Any]:
        """Simulate a Twilio webhook callback"""
        data = {
            "CallSid": call_sid,
            "CallStatus": status,
            "CallDuration": duration
        }
        resp = await self.client.post("/webhooks/twilio/status", data=data)
        return resp.json()

    async def test_a_long_hold_voicemail(self):
        """Test A: Long-hold & voicemail behavior with watchdog"""
        print("\n=== Test A: Long-hold & Voicemail Behavior (Watchdog) ===")
        
        # Check if watchdog is enabled
        watchdog_enabled = os.getenv("ENABLE_WATCHDOG", "false").lower() == "true"
        max_hold_seconds = int(os.getenv("MAX_HOLD_SECONDS", "5"))
        
        print(f"Watchdog enabled: {watchdog_enabled}")
        print(f"Max hold seconds: {max_hold_seconds}")
        
        if not watchdog_enabled:
            print("⚠️  Watchdog not enabled - skipping timeout test")
            print("   Set ENABLE_WATCHDOG=true to test")
            return
        
        try:
            # Create task for long-hold test
            task = await self.create_task("LongHold", "long_hold")
            task_id = task["task_id"]
            print(f"Created task {task_id} for long-hold test")
            
            # Trigger call
            call_result = await self.trigger_call(task_id)
            print(f"Call triggered: {call_result}")
            
            # Wait for watchdog to kick in (max_hold + buffer for next check)
            wait_time = max_hold_seconds + 65  # Add 65s for next watchdog cycle
            print(f"Waiting {wait_time}s for watchdog to process...")
            await asyncio.sleep(wait_time)
            
            # Check task status after watchdog
            task_detail = await self.get_task(task_id)
            
            # Verify watchdog moved it out of CALLING state
            if task_detail["status"] == "CALLING":
                print(f"❌ Task still in CALLING state after {wait_time}s")
                test_results["A_long_hold"]["fail"] += 1
                test_results["A_long_hold"]["details"].append(
                    f"Watchdog failed - task {task_id} still CALLING"
                )
            elif task_detail["status"] in ["NO_ANSWER_RETRY", "FAILED"]:
                print(f"✅ Watchdog worked - status: {task_detail['status']}")
                test_results["A_long_hold"]["pass"] += 1
                
                # Check outcome v2 fields if enabled
                if os.getenv("ENABLE_OUTCOME_V2", "false").lower() == "true":
                    if task_detail.get("outcome_code"):
                        print(f"✅ Outcome code: {task_detail['outcome_code']}")
                        print(f"✅ Outcome note: {task_detail['outcome_note']}")
                        test_results["A_long_hold"]["pass"] += 1
                    else:
                        print("❌ Outcome v2 fields not populated")
                        test_results["A_long_hold"]["fail"] += 1
            else:
                print(f"⚠️  Unexpected status: {task_detail['status']}")
                
        except Exception as e:
            print(f"❌ Long-hold test failed: {e}")
            test_results["A_long_hold"]["fail"] += 1
            test_results["A_long_hold"]["details"].append(str(e))

    async def test_b_concurrency(self):
        """Test B: Concurrency & scaling"""
        print("\n=== Test B: Concurrency & Scaling ===")
        
        concurrent_count = 25
        tasks = []
        start_time = time.time()
        
        try:
            # Create 25 concurrent tasks
            print(f"Creating {concurrent_count} concurrent tasks...")
            create_tasks = []
            for i in range(concurrent_count):
                create_tasks.append(self.create_task(f"Concurrent{i}", "concurrent"))
            
            created = await asyncio.gather(*create_tasks, return_exceptions=True)
            
            # Check for duplicates or errors
            task_ids = set()
            for i, result in enumerate(created):
                if isinstance(result, Exception):
                    print(f"❌ Task {i} creation failed: {result}")
                    test_results["B_concurrency"]["fail"] += 1
                else:
                    if result["task_id"] in task_ids:
                        print(f"❌ Duplicate task ID: {result['task_id']}")
                        test_results["B_concurrency"]["fail"] += 1
                    else:
                        task_ids.add(result["task_id"])
                        tasks.append(result["task_id"])
            
            print(f"Created {len(tasks)} unique tasks")
            
            # Trigger all calls concurrently
            print("Triggering concurrent calls...")
            call_tasks = []
            for task_id in tasks:
                call_tasks.append(self.trigger_call(task_id))
            
            call_results = await asyncio.gather(*call_tasks, return_exceptions=True)
            
            successful_calls = sum(1 for r in call_results if not isinstance(r, Exception))
            print(f"Successfully triggered {successful_calls}/{len(tasks)} calls")
            
            # Wait for processing
            await asyncio.sleep(5)
            
            # Check final states
            final_states = {"RESOLVED": 0, "FAILED": 0, "NO_ANSWER_RETRY": 0, "CALLING": 0, "QUEUED": 0}
            
            check_tasks = []
            for task_id in tasks:
                check_tasks.append(self.get_task(task_id))
            
            task_details = await asyncio.gather(*check_tasks, return_exceptions=True)
            
            for detail in task_details:
                if not isinstance(detail, Exception):
                    status = detail.get("status", "UNKNOWN")
                    if status in final_states:
                        final_states[status] += 1
            
            elapsed = time.time() - start_time
            
            print(f"\nConcurrency test completed in {elapsed:.2f} seconds")
            print(f"Final states: {json.dumps(final_states, indent=2)}")
            
            # Verify expectations
            if elapsed < 240:  # Should complete within 4 minutes
                print("✅ Completed within 4 minutes")
                test_results["B_concurrency"]["pass"] += 1
            else:
                print("❌ Took too long to complete")
                test_results["B_concurrency"]["fail"] += 1
                
            if final_states["CALLING"] == 0:
                print("✅ No tasks stuck in CALLING state")
                test_results["B_concurrency"]["pass"] += 1
            else:
                print(f"❌ {final_states['CALLING']} tasks stuck in CALLING")
                test_results["B_concurrency"]["fail"] += 1
                
        except Exception as e:
            print(f"❌ Concurrency test failed: {e}")
            test_results["B_concurrency"]["fail"] += 1
            test_results["B_concurrency"]["details"].append(str(e))

    async def test_c_outcomes(self):
        """Test C: Outcome fidelity"""
        print("\n=== Test C: Outcome Fidelity ===")
        
        try:
            # Create test task
            task = await self.create_task("Outcome", "outcome_test")
            task_id = task["task_id"]
            
            # Trigger call
            await self.trigger_call(task_id)
            await asyncio.sleep(2)
            
            # Get task details
            detail = await self.get_task(task_id)
            
            # Check outcome fields
            missing_fields = []
            
            # Check for status
            if not detail.get("status"):
                missing_fields.append("status")
                
            # Check timestamps
            if not detail.get("created_at"):
                missing_fields.append("created_at")
                
            # For completed tasks, check completion timestamp
            if detail.get("status") in ["RESOLVED", "FAILED"] and not detail.get("updated_at"):
                missing_fields.append("updated_at")
            
            if missing_fields:
                print(f"❌ Missing outcome fields: {missing_fields}")
                test_results["C_outcomes"]["fail"] += 1
            else:
                print("✅ All outcome fields present")
                test_results["C_outcomes"]["pass"] += 1
                
            # Test summary endpoint (if exists)
            try:
                resp = await self.client.get("/api/reports/location-summary")
                if resp.status_code == 200:
                    print("✅ Summary endpoint available")
                    test_results["C_outcomes"]["pass"] += 1
                else:
                    print("⚠️  Summary endpoint not implemented")
            except:
                print("⚠️  Summary endpoint not available")
                
        except Exception as e:
            print(f"❌ Outcome test failed: {e}")
            test_results["C_outcomes"]["fail"] += 1

    async def test_d_guardrails(self):
        """Test D: Operational guardrails"""
        print("\n=== Test D: Operational Guardrails ===")
        
        guardrails_status = {
            "business_hours": False,
            "retry_backoff": False,
            "daily_limits": False,
            "idempotency": False,
            "rls": False
        }
        
        try:
            # Test 1: Business hours check
            from utils.schedule import is_business_hours, validate_call_timing
            
            # Test weekend
            weekend = datetime(2024, 1, 6, 10, 0)  # Saturday
            if not is_business_hours(weekend):
                print("✅ Business hours: Weekend blocked")
                guardrails_status["business_hours"] = True
                test_results["D_guardrails"]["pass"] += 1
            else:
                print("❌ Business hours: Weekend not blocked")
                test_results["D_guardrails"]["fail"] += 1
            
            # Test 2: Idempotency
            task1 = await self.create_task("Idempotent", "idem_test")
            task2 = await self.create_task("Idempotent", "idem_test")
            
            if task1["task_id"] == task2["task_id"] and task2.get("idempotency_hit"):
                print("✅ Idempotency: Duplicate prevented")
                guardrails_status["idempotency"] = True
                test_results["D_guardrails"]["pass"] += 1
            else:
                print("❌ Idempotency: Duplicate not prevented")
                test_results["D_guardrails"]["fail"] += 1
            
            # Test 3: Daily limits
            can_call, reason = validate_call_timing(
                datetime.now(timezone.utc),
                "+19195551234",
                daily_attempts=3
            )
            if not can_call and "limit" in reason:
                print("✅ Daily limits: Enforced at 3/day")
                guardrails_status["daily_limits"] = True
                test_results["D_guardrails"]["pass"] += 1
            else:
                print("❌ Daily limits: Not properly enforced")
                test_results["D_guardrails"]["fail"] += 1
            
            # Summary
            print(f"\nGuardrails status: {json.dumps(guardrails_status, indent=2)}")
            
        except Exception as e:
            print(f"❌ Guardrails test failed: {e}")
            test_results["D_guardrails"]["fail"] += 1

    async def test_e_backwards_compatibility(self):
        """Test E: Backwards compatibility with flags OFF"""
        print("\n=== Test E: Backwards Compatibility ===")
        
        # Save current flags
        original_watchdog = os.environ.get("ENABLE_WATCHDOG", "false")
        original_outcome = os.environ.get("ENABLE_OUTCOME_V2", "false")
        
        try:
            # Disable all new features
            os.environ["ENABLE_WATCHDOG"] = "false"
            os.environ["ENABLE_OUTCOME_V2"] = "false"
            
            print("Running with all flags OFF...")
            
            # Create and trigger a task
            task = await self.create_task("Compat", "backwards_compat")
            task_id = task["task_id"]
            
            # Trigger call
            await self.trigger_call(task_id)
            await asyncio.sleep(3)
            
            # Get task details
            task_detail = await self.get_task(task_id)
            
            # Verify no new fields are populated
            if task_detail.get("outcome_code") is None and task_detail.get("outcome_note") is None:
                print("✅ No outcome v2 fields populated (expected)")
                test_results["E_backwards"]["pass"] += 1
            else:
                print("❌ Outcome v2 fields populated when disabled")
                test_results["E_backwards"]["fail"] += 1
                
            # Verify status behavior unchanged
            if task_detail["status"] in ["QUEUED", "CALLING", "RESOLVED", "FAILED", "NO_ANSWER_RETRY"]:
                print("✅ Status behavior unchanged")
                test_results["E_backwards"]["pass"] += 1
            else:
                print(f"❌ Unexpected status: {task_detail['status']}")
                test_results["E_backwards"]["fail"] += 1
                
        except Exception as e:
            print(f"❌ Backwards compatibility test failed: {e}")
            test_results["E_backwards"]["fail"] += 1
        finally:
            # Restore flags
            os.environ["ENABLE_WATCHDOG"] = original_watchdog
            os.environ["ENABLE_OUTCOME_V2"] = original_outcome
    
    async def run_all_tests(self):
        """Run all reliability tests"""
        print(f"""
=================================================
    CLINICWIRE CORE RELIABILITY TEST SUITE
    Running with SIMULATE={SIMULATE}
    Backend: {BASE_URL}
    Feature Flags:
      ENABLE_WATCHDOG={os.getenv("ENABLE_WATCHDOG", "false")}
      ENABLE_OUTCOME_V2={os.getenv("ENABLE_OUTCOME_V2", "false")}
      MAX_HOLD_SECONDS={os.getenv("MAX_HOLD_SECONDS", "1200")}
=================================================
        """)
        
        # Add backwards compatibility test results
        test_results["E_backwards"] = {"pass": 0, "fail": 0, "details": []}
        
        await self.test_a_long_hold_voicemail()
        await self.test_b_concurrency()
        await self.test_c_outcomes()
        await self.test_d_guardrails()
        await self.test_e_backwards_compatibility()
        
        # Generate summary report
        print("\n=== SUMMARY REPORT ===")
        print(f"Generated: {datetime.now().isoformat()}")
        print(f"\nTest Results:")
        
        all_passed = True
        for section, results in test_results.items():
            passed = results["pass"]
            failed = results["fail"]
            total = passed + failed
            status = "✅ PASS" if failed == 0 else "❌ FAIL"
            
            print(f"\n{section}: {status} ({passed}/{total} passed)")
            if results["details"]:
                for detail in results["details"]:
                    print(f"  - {detail}")
            
            if failed > 0:
                all_passed = False
        
        print(f"\n{'='*50}")
        print(f"Overall: {'✅ ALL TESTS PASSED' if all_passed else '❌ SOME TESTS FAILED'}")
        print(f"{'='*50}\n")
        
        return all_passed


async def main():
    """Main entry point"""
    async with TestHarness() as harness:
        success = await harness.run_all_tests()
        sys.exit(0 if success else 1)


if __name__ == "__main__":
    asyncio.run(main())