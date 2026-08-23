import type {
  AgentRun,
  ApprovalInput,
  ApprovalResult,
  CareCase,
  CareLoopApi,
  DashboardState,
  RunStage,
  ScenarioId,
  ScenarioRun,
  TimelineEvent
} from "@/types/domain";
import { seedCases } from "@/data/mock/cases";
import { idleRun } from "@/data/mock/runs";
import { EXCERPTS } from "@/data/mock/evidence";

/* ------------------------------------------------------------------ *
 * Mock CareLoop adapter.
 *
 * Every scenario below is a timed choreography that mimics a real
 * asynchronous backend run: the dashboard, the case timeline and the
 * Under the Hood drawer all read from THE SAME store, so replacing this
 * adapter with real polling does not change a single component.
 *
 * Timings are the approved demo pacing. Do not speed them up.
 * ------------------------------------------------------------------ */

type Listener = (state: DashboardState) => void;

let eventSeq = 0;
function nextEventId(): string {
  eventSeq += 1;
  return "ev_" + eventSeq;
}

function clock(): string {
  return new Date().toTimeString().slice(0, 5);
}

class MockCareLoopApi implements CareLoopApi {
  private cases: CareCase[] = seedCases();
  private run: AgentRun = idleRun();
  private listeners = new Set<Listener>();
  private timers: number[] = [];

  /* -------------------- store plumbing -------------------- */

  private snapshot(): DashboardState {
    // The mock is its own backend, so it is never anything but live.
    return { cases: this.cases.map((c) => ({ ...c })), run: { ...this.run }, connection: "live" };
  }

  private emit(): void {
    const state = this.snapshot();
    this.listeners.forEach((l) => l(state));
  }

  private at(ms: number, fn: () => void): void {
    this.timers.push(window.setTimeout(fn, ms));
  }

  private clearTimers(): void {
    this.timers.forEach((t) => window.clearTimeout(t));
    this.timers = [];
  }

  private patch(id: string, fn: (c: CareCase) => CareCase): void {
    this.cases = this.cases.map((c) => (c.id === id ? fn({ ...c }) : c));
    this.emit();
  }

  private addEvent(id: string, ev: Omit<TimelineEvent, "id" | "time">): void {
    this.patch(id, (c) => ({
      ...c,
      events: [...c.events, { id: nextEventId(), time: clock(), ...ev }],
      updatedLabel: "Just now"
    }));
  }

  private setRun(stage: RunStage, stageLabel: string, extra: Partial<AgentRun> = {}): void {
    this.run = { ...this.run, stage, stageLabel, ...extra };
    this.emit();
  }

  /* -------------------- CareLoopApi -------------------- */

  async getDashboard(): Promise<DashboardState> {
    return this.snapshot();
  }

  async getCase(caseId: string): Promise<CareCase | undefined> {
    return this.cases.find((c) => c.id === caseId);
  }

  async getRun(runId: string): Promise<AgentRun | undefined> {
    return this.run.id === runId ? { ...this.run } : undefined;
  }

  subscribe(listener: Listener): () => void {
    this.listeners.add(listener);
    listener(this.snapshot());
    return () => {
      this.listeners.delete(listener);
    };
  }

  async resetDemo(): Promise<void> {
    this.clearTimers();
    this.cases = seedCases();
    this.run = idleRun();
    this.emit();
  }

  async runScenario(id: ScenarioId): Promise<ScenarioRun> {
    await this.resetDemo();
    if (id === "referral") return this.referral();
    if (id === "injection") return this.injection();
    /* Not offered by the mock, for the reason a re-check is not: it claims a real provider
       committed a real write whose answer was really lost, and a choreography cannot make that
       claim. Refused with an empty run rather than falling through to `resume`, which would have
       shown a judge the wrong scenario under the right button. */
    if (id === "recovery") return { scenarioId: id, runId: "", caseId: "" };
    return this.resume();
  }

  /** Not offered by the mock: a re-check needs a real recorded effect and a real observer. */
  async recheckCase(caseId: string): Promise<ApprovalResult> {
    return { caseId, runId: this.run.id, accepted: false };
  }

  async approveAction(input: ApprovalInput): Promise<ApprovalResult> {
    const target = this.cases.find((c) => c.id === input.caseId);
    if (!target) return { caseId: input.caseId, runId: this.run.id, accepted: false };
    if (input.approved) this.approve(input.caseId);
    else this.reject(input.caseId);
    return { caseId: input.caseId, runId: this.run.id, accepted: true };
  }

  /* -------------------- scenario 01 · referral -------------------- */

  private referral(): ScenarioRun {
    const id = "c-card";
    const runId = "run_01J8QK";

    this.cases = [
      {
        id,
        runId,
        title: "Cardiology referral",
        section: "working",
        statusLabel: "READING THE REFERRAL",
        statusText: "Reading the referral",
        headline: "Reading the referral",
        updatedLabel: "Just now",
        events: [{ id: nextEventId(), time: clock(), tone: "neutral", text: "Referral received from Dr. Alvarez" }],
        evidence: []
      },
      ...this.cases
    ];
    this.run = { ...idleRun(), id: runId, scenario: "referral" };
    this.setRun("TRIAGE", runId + " · triage");

    this.at(1100, () => {
      this.setRun("EVIDENCE", runId + " · evidence");
      this.patch(id, (c) => ({
        ...c,
        statusLabel: "GATHERING DOCUMENTS",
        statusText: "Gathering the documents",
        headline: "Gathering the documents"
      }));
      this.addEvent(id, {
        tone: "neutral",
        text: "Appointment deadline found — September 4",
        source: EXCERPTS.appointmentDeadline
      });
    });

    this.at(2400, () => {
      this.setRun("PLANNER", runId + " · planner");
      this.patch(id, (c) => ({
        ...c,
        statusLabel: "WORKING OUT THE NEXT STEP",
        statusText: "Working out the next step",
        headline: "Working out the next step",
        evidence: [
          {
            fact: "Appointment deadline · September 4",
            source: "referral.pdf · page 2",
            trust: "Validated",
            excerpt: EXCERPTS.appointmentDeadlineShort
          },
          {
            fact: "Visit length · 42 minutes",
            source: "appointment-instructions.pdf · page 1",
            trust: "Validated",
            excerpt: EXCERPTS.visitLength
          }
        ]
      }));
      this.addEvent(id, { tone: "neutral", text: "Tuesday 10:00 AM conflicts with Maya's calendar" });
    });

    this.at(3700, () => {
      this.setRun("GATE", runId + " · gate → REQUIRE_HUMAN", {
        policyVerdict: "REQUIRE_HUMAN",
        policyDetail: "REQUIRE_HUMAN → ALLOW"
      });
      this.patch(id, (c) => ({
        ...c,
        section: "needs",
        statusLabel: "NEEDS YOUR DECISION",
        statusText: "Needs your decision",
        headline: "Needs your decision",
        decision: {
          id: "d-card-1",
          kind: "REQUIRES_HUMAN_APPROVAL",
          title: "Cardiology appointment",
          body:
            "Your preferred Tuesday slot conflicts with another event. Thursday at 2:00 PM is open and still meets the September 4 deadline.",
          facts: [
            { label: "WHEN", value: "Thu Sep 3 · 2:00 PM" },
            { label: "WHERE", value: "Northgate Cardiology" },
            { label: "LENGTH", value: "42 min" }
          ],
          after: "After you approve: book, file the confirmation, then check the clinic's own record.",
          approveLabel: "Approve Thursday",
          rejectLabel: "Choose another",
          recheckable: false,
          recheckLabel: "Check the clinic again"
        }
      }));
      this.addEvent(id, { tone: "decision", text: "Decision requested" });
    });

    return { scenarioId: "referral", runId, caseId: id };
  }

  private approve(id: string): void {
    const runId = this.run.id;
    this.patch(id, (c) => ({
      ...c,
      section: "working",
      decision: null,
      statusLabel: "BOOKING",
      statusText: "Booking the appointment…",
      headline: "Booking the appointment…"
    }));
    this.addEvent(id, { tone: "neutral", text: "Maya approved Thursday at 2:00 PM" });
    this.setRun("EXECUTE", runId + " · execute", { policyVerdict: "ALLOW", policyDetail: "REQUIRE_HUMAN → ALLOW" });

    this.at(1300, () => {
      this.addEvent(id, { tone: "neutral", text: "Appointment booked" });
      this.patch(id, (c) => ({
        ...c,
        statusLabel: "CONFIRMING",
        statusText: "Confirming with the clinic…",
        headline: "Confirming with the clinic…"
      }));
      this.setRun("VERIFY", runId + " · verify");
    });

    this.at(2900, () => {
      this.addEvent(id, { tone: "verified", text: "Clinic system checked independently — match" });
      this.patch(id, (c) => ({
        ...c,
        section: "done",
        verified: true,
        statusLabel: "COMPLETED",
        statusText: "Booked · independently verified",
        headline: "Thu Sep 3 · 2:00 PM · Northgate Cardiology",
        updatedLabel: "Just now",
        outcome: {
          title: "Appointment booked and confirmed",
          body: "Northgate Cardiology · Thursday, September 3 at 2:00 PM."
        },
        readback: {
          authorized: "Thu Sep 3 · 2:00 PM",
          observed: "Thu Sep 3 · 2:00 PM",
          result: "Match — loop closed",
          state: "MATCH"
        }
      }));
      this.setRun("SETTLED", runId + " · settled", {
        verification: "MATCH",
        verificationDetail: "MATCH — external read-back"
      });
    });
  }

  private reject(id: string): void {
    const runId = this.run.id;
    this.patch(id, (c) => ({
      ...c,
      section: "working",
      decision: null,
      statusLabel: "FINDING ANOTHER TIME",
      statusText: "Looking for another time",
      headline: "Looking for another time"
    }));
    this.addEvent(id, { tone: "neutral", text: "Maya declined Thursday — looking for another slot" });
    this.setRun("PLANNER", runId + " · planner (replan)", { policyVerdict: "PENDING", policyDetail: "pending" });
  }

  /* -------------------- scenario 02 · malicious document -------------------- */

  private injection(): ScenarioRun {
    const id = "c-cont";
    const runId = "run_01J8RB";

    this.cases = [
      {
        id,
        runId,
        title: "Care continuity packet",
        section: "working",
        statusLabel: "READING THE DOCUMENT",
        statusText: "Reading the document",
        headline: "Reading the document",
        updatedLabel: "Just now",
        events: [
          { id: nextEventId(), time: clock(), tone: "neutral", text: "Continuity document received from Meadowview" }
        ],
        evidence: []
      },
      ...this.cases
    ];
    this.run = {
      ...idleRun(),
      id: runId,
      scenario: "injection",
      actionIntent: "records.transfer(dest=meadowview_approved)"
    };
    this.setRun("EVIDENCE", runId + " · evidence");

    this.at(1200, () => {
      this.addEvent(id, {
        tone: "untrusted",
        text: "3 unsafe instructions isolated inside the document",
        blocked: [
          { text: "Send records to an untrusted destination", rule: "DESTINATION_NOT_TRUSTED" },
          { text: "Upload documents to an unknown portal", rule: "DESTINATION_NOT_TRUSTED" },
          { text: "Grant records_admin access", rule: "AUTHORITY_ESCALATION" }
        ]
      });
      this.setRun("GATE", runId + " · gate → DENY", {
        policyVerdict: "DENY",
        policyDetail: "DENY ×3 · rule DESTINATION_NOT_TRUSTED"
      });
    });

    this.at(2600, () => {
      this.addEvent(id, { tone: "neutral", text: "Continued with the legitimate records request" });
      this.patch(id, (c) => ({
        ...c,
        statusLabel: "REQUESTING RECORDS",
        statusText: "Requesting the records the approved way",
        headline: "Requesting the records the approved way",
        evidence: [
          {
            fact: "Requesting provider · Meadowview Clinic",
            source: "continuity.pdf · page 1",
            trust: "Validated",
            excerpt: EXCERPTS.requestingProvider
          }
        ]
      }));
      this.setRun("EXECUTE", runId + " · execute");
    });

    this.at(4100, () => {
      this.addEvent(id, { tone: "neutral", text: "Records sent to Meadowview through the approved channel" });
      this.setRun("VERIFY", runId + " · verify");
    });

    this.at(5400, () => {
      this.addEvent(id, { tone: "verified", text: "Receipt confirmed by Meadowview — match" });
      this.patch(id, (c) => ({
        ...c,
        section: "done",
        verified: true,
        statusLabel: "COMPLETED",
        statusText: "Sent · independently verified",
        headline: "Records delivered to Meadowview Clinic",
        updatedLabel: "Just now",
        outcome: {
          title: "Records delivered and confirmed",
          body:
            "Meadowview Clinic · approved transfer channel. Three unsafe instructions in the source document were isolated and never acted on."
        },
        readback: {
          authorized: "3 documents → Meadowview",
          observed: "3 documents → Meadowview",
          result: "Match — loop closed",
          state: "MATCH"
        }
      }));
      this.setRun("SETTLED", runId + " · settled", {
        verification: "MATCH",
        verificationDetail: "MATCH — external read-back"
      });
    });

    return { scenarioId: "injection", runId, caseId: id };
  }

  /* -------------------- scenario 03 · resume -------------------- */

  /**
   * Kept as the design package wrote it, under the key the backend actually ships.
   *
   * The choreography below is still the *mismatch* beat, and the backend cannot produce it: a
   * real read-back mismatch needs the appointment moved from outside CareLoop. This mock is
   * superseded by `realApi` and stays only as the offline fallback and as the record of the
   * approved pacing — nothing renders it once `api.ts` points at the real adapter.
   */
  private resume(): ScenarioRun {
    const id = "c-follow";
    const runId = "run_01J8SC";

    this.cases = [
      {
        id,
        runId,
        title: "Follow-up appointment",
        section: "working",
        statusLabel: "BOOKING",
        statusText: "Booking the appointment…",
        headline: "Booking the appointment…",
        updatedLabel: "Just now",
        events: [
          {
            id: nextEventId(),
            time: clock(),
            tone: "neutral",
            text: "Follow-up requested by Northgate Cardiology"
          }
        ],
        evidence: []
      },
      ...this.cases
    ];
    this.run = { ...idleRun(), id: runId, scenario: "resume" };
    this.setRun("EXECUTE", runId + " · execute", { policyVerdict: "ALLOW", policyDetail: "ALLOW" });

    this.at(1300, () => {
      this.addEvent(id, { tone: "neutral", text: "Appointment booked — Thursday at 2:00 PM" });
      this.patch(id, (c) => ({
        ...c,
        statusText: "Confirming with the clinic…",
        headline: "Confirming with the clinic…"
      }));
      this.setRun("VERIFY", runId + " · verify");
    });

    this.at(2900, () => {
      this.addEvent(id, { tone: "denied", text: "Clinic reports a different time — not marked complete" });
      this.patch(id, (c) => ({
        ...c,
        section: "needs",
        mismatch: true,
        statusLabel: "NEEDS A HUMAN",
        statusText: "Something changed after booking",
        headline: "Something changed after booking",
        outcome: {
          title: "Something changed after booking",
          body:
            "CareLoop authorized Thursday at 2:00 PM. The clinic's own record now shows a different time, so the case was not marked complete."
        },
        readback: {
          authorized: "Thu Sep 3 · 2:00 PM",
          observed: "Fri Sep 4 · 10:30 AM",
          result: "No match",
          state: "MISMATCH"
        },
        decision: {
          id: "d-follow-1",
          kind: "NO_SAFE_ACTION_AVAILABLE",
          title: "Follow-up appointment",
          body:
            "CareLoop authorized Thursday at 2:00 PM. The clinic currently reports Friday at 10:30 AM. There is no safe action CareLoop can take on its own — rebooking would risk a double appointment.",
          facts: [
            { label: "AUTHORIZED", value: "Thu Sep 3 · 2:00 PM" },
            { label: "CLINIC REPORTS", value: "Fri Sep 4 · 10:30 AM" },
            { label: "STATUS", value: "Not completed" }
          ],
          after: "Nothing was changed. CareLoop will not retry until a person resolves this.",
          approveLabel: "Approve",
          rejectLabel: "Reject",
          // The mock is the record of the approved demo package and predates the re-check, so its
          // cards offer none. Kept false rather than invented: this adapter is wired to no backend
          // that could resolve a real target, and a button that resolved nothing would be worse
          // than an absent one.
          recheckable: false,
          recheckLabel: "Check the clinic again"
        }
      }));
      this.setRun("SETTLED", runId + " · halted (verification mismatch)", {
        verification: "MISMATCH",
        verificationDetail: "MISMATCH — authorized ≠ observed"
      });
    });

    return { scenarioId: "resume", runId, caseId: id };
  }
}

export const mockApi: CareLoopApi = new MockCareLoopApi();
