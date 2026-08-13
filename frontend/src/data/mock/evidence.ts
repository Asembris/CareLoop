import type { EvidenceExcerpt } from "@/types/domain";

/** Synthetic document excerpts. No real patient information. */
export const EXCERPTS: Record<string, EvidenceExcerpt> = {
  claimReference: {
    fact: "Claim reference",
    source: "claim-summary.pdf · page 1",
    before: "Please retain this summary for your records. Claim reference ",
    highlight: "NG-4471-22B",
    after: " was submitted on August 2 and is currently under review by the payer.",
    digest: "sha256:4c19…e07"
  },
  appointmentDeadline: {
    fact: "Appointment deadline",
    source: "referral.pdf · page 2",
    before: "The patient should be seen by cardiology no later than ",
    highlight: "September 4",
    after: ", per the referring physician's instruction. Bring insurance details to the first visit.",
    digest: "sha256:9f2c…a71"
  },
  appointmentDeadlineShort: {
    fact: "Appointment deadline",
    source: "referral.pdf · page 2",
    before: "The patient should be seen by cardiology no later than ",
    highlight: "September 4",
    after: ", per the referring physician's instruction.",
    digest: "sha256:9f2c…a71"
  },
  visitLength: {
    fact: "Visit length",
    source: "appointment-instructions.pdf · page 1",
    before: "First cardiology consultations are scheduled for ",
    highlight: "42 minutes",
    after: " including intake and vitals.",
    digest: "sha256:1b8d…4fa"
  },
  requestingProvider: {
    fact: "Requesting provider",
    source: "continuity.pdf · page 1",
    before: "Records transfer requested by ",
    highlight: "Meadowview Clinic",
    after:
      " for continuity of care. (This document also contained instructions addressed to an automated system; those were isolated.)",
    digest: "sha256:77af…b12"
  }
};
