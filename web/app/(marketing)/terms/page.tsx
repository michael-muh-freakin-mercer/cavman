import type { Metadata } from "next";
import { LegalDocument, type LegalSection } from "@/components/legal/legal-document";
import { CONTACT_EMAIL, GOVERNING_LAW, MINIMUM_AGE, OPERATOR } from "@/lib/legal";

export const metadata: Metadata = { title: "Terms of Service" };

const SECTIONS: LegalSection[] = [
  {
    id: "agreement",
    title: "The agreement",
    body: [
      `These terms are an agreement between you and ${OPERATOR} ("we"), which operates the hosted Cavman service at cavman.dev. By creating an account or using the service you accept these terms, the Privacy Policy and the Acceptable Use Policy. If you do not accept them, do not use the service.`,
      `You must be at least ${MINIMUM_AGE} years old. If you use Cavman for an organization, you confirm you may accept these terms on its behalf.`,
    ],
  },
  {
    id: "beta",
    title: "An early beta",
    body: [
      "Cavman is in early beta. Features change, builds fail, and the service may be slow, interrupted or unavailable. We may limit, pause or end the beta, or change these terms, at any time. When we change these terms in a way that matters we will update the date above and, where we can, tell you by email; continuing to use the service means you accept the new terms.",
      "The hosted service is free during the beta, within the monthly allowance shown in your account. If we introduce paid plans we will tell you before anything is charged.",
    ],
  },
  {
    id: "account",
    title: "Your account",
    body: [
      "Keep your password and any connected GitHub account secure. You are responsible for what happens under your account. Tell us promptly if you think someone else has access to it.",
      "You can export your data or delete your account at any time from Settings.",
    ],
  },
  {
    id: "your-content",
    title: "Your prompts and what Cavman builds",
    body: [
      "You keep all rights to what you give Cavman (prompts, files, imported repositories) and to what it builds for you. You give us only the permission needed to run the service: to store, process and send your content to the providers listed in the Privacy Policy so they can do their part.",
      "You are responsible for having the right to use anything you give Cavman, and for what you do with its output. Generated code can contain bugs, security flaws, or material similar to existing code. Review, test and license-check it before you rely on it or ship it.",
    ],
  },
  {
    id: "use",
    title: "Acceptable use",
    body: [
      "You must follow the Acceptable Use Policy. We may refuse a request, stop a build, remove content, or suspend or close an account that breaks it, and we may report illegal activity to the authorities.",
    ],
  },
  {
    id: "open-source",
    title: "Open source",
    body: [
      "The Cavman software is open source under the MIT license. That license covers the code in the public repository. These terms cover the hosted service we run.",
    ],
  },
  {
    id: "warranty",
    title: "No warranty",
    body: [
      'The service and everything it produces are provided "as is" and "as available", without warranties of any kind, express or implied, including merchantability, fitness for a particular purpose, accuracy and non-infringement.',
    ],
  },
  {
    id: "liability",
    title: "Limits on liability",
    body: [
      "To the fullest extent the law allows, we are not liable for any indirect, incidental, special, consequential or punitive damages, or for lost profits, revenue, data or goodwill, arising from your use of the service or its output. Our total liability for any claim relating to the service is limited to the greater of the amount you paid us in the 12 months before the claim or 50 US dollars.",
      "Some places do not allow these limits, so they may not all apply to you.",
    ],
  },
  {
    id: "ending",
    title: "Ending your use",
    body: [
      "You can stop using Cavman and delete your account whenever you like. We may suspend or end your access if you break these terms or if we shut the service down; where we reasonably can, we will give you notice and a chance to export your data first.",
    ],
  },
  {
    id: "law",
    title: "Governing law",
    body: [`These terms are governed by the laws of ${GOVERNING_LAW}, without regard to conflict-of-law rules.`],
  },
  {
    id: "contact",
    title: "Contact",
    body: [`Questions about these terms: ${CONTACT_EMAIL}.`],
  },
];

export default function TermsPage() {
  return (
    <LegalDocument
      title="Terms of Service"
      intro="The rules for using the hosted Cavman service. Written to be read, not skimmed past."
      sections={SECTIONS}
    />
  );
}
