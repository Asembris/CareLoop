import type { CareCase } from "@/types/domain";
import { EXCERPTS } from "./evidence";

/** Baseline dashboard state before any scenario runs.
 *  Replaced by GET /api/cases once the real adapter is wired. */
export function seedCases(): CareCase[] {
  return [
    {
      id: "c-reimb",
      runId: "run_01J8PA",
      title: "Reimbursement follow-up",
      section: "working",
      statusLabel: "WAITING FOR A REPLY",
      statusText: "Waiting for the provider's document",
      headline: "Waiting for the provider's document",
      updatedLabel: "14 min ago",
      events: [
        { id: "e1", time: "09:12", tone: "neutral", text: "Claim receipt requested from Northgate billing" },
        { id: "e2", time: "09:12", tone: "neutral", text: "Reference number found", source: EXCERPTS.claimReference },
        { id: "e3", time: "11:40", tone: "neutral", text: "Checked again — no reply yet" }
      ],
      evidence: []
    },
    {
      id: "c-packet",
      runId: "run_01J8NC",
      title: "Referral packet",
      section: "done",
      statusLabel: "COMPLETED",
      statusText: "Filed · independently verified",
      headline: "Filed with the clinic",
      updatedLabel: "Yesterday",
      verified: true,
      outcome: {
        title: "Packet filed and confirmed",
        body: "Northgate Cardiology confirmed receipt of the complete packet."
      },
      readback: {
        authorized: "3 documents · Aug 11",
        observed: "3 documents · Aug 11",
        result: "Match",
        state: "MATCH"
      },
      events: [
        { id: "e1", time: "16:02", tone: "neutral", text: "Referral packet assembled" },
        { id: "e2", time: "16:03", tone: "neutral", text: "Sent to Northgate Cardiology" },
        { id: "e3", time: "16:05", tone: "verified", text: "Clinic record checked independently — match" }
      ],
      evidence: []
    }
  ];
}
