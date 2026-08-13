import type { AgentRun } from "@/types/domain";

export function idleRun(): AgentRun {
  return {
    id: "run_idle",
    scenario: null,
    stage: "IDLE",
    stageLabel: "idle",
    actionIntent: "clinic.book(slot=2025-09-03T14:00, dur=42m)",
    idempotencyKey: "ik_01J8QK7ZV3M2C4TQ",
    policyVerdict: "PENDING",
    policyDetail: "pending",
    evidenceDigest: "sha256:9f2c…a71 (referral.pdf p2)",
    verification: "PENDING",
    verificationDetail: "pending"
  };
}
