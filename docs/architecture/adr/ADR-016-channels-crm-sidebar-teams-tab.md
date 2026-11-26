# ADR-016: Channels: CRM sidebar and Teams personal tab first, Teams bot after residency sign-off

| Field | Value |
|---|---|
| Status | Accepted (Phase 1 channels); Teams bot is Proposed for Phase 2, gated on C1 |
| Date | 26 Nov 2026 |
| Deciders | Solution architecture, SME business owner, InfoSec, Microsoft 365 platform team |
| Critical issues | C1 (bot traffic residency), C18 (verified release), C19 (output rendering), C20 (Graph scopes) |
| Phase | 1, bot in Phase 2 |

## Context

RMs live in two tools: the CRM (where customer records, call reports and tasks are) and Microsoft Teams (where
they spend the rest of the day). The copilot must appear where the work happens, keep the identity chain intact
(§3.4), and keep every byte of customer data in the UAE. Forces:

- **Residency (C1).** Teams bots receive and send messages through the Bot Framework connector service, which is
  a global service; whether bot traffic can be processed in the UAE is unconfirmed **[confirm, §10 item 3]**.
- **Private ingress.** The only ingress is the internal Application Gateway WAF, reached from managed devices on
  the corporate network or through Entra Private Access (§3.3 Edge row). A bot needs a messaging endpoint the
  Bot Framework service can call, which implies a public listener.
- **Verified release (C18, ADR-022) and plain-text rendering (C19).** The channel must show exactly what the
  verification step released, with sources, with no Markdown images or external links.
- **Write approval (ADR-009).** The approve action needs Origin and CSRF checks and step-up authentication; it is
  a web interaction, not a chat message.
- **Graph scopes (C20).** `Calendars.ReadBasic` only; the email draft opens in Outlook via a compose deeplink.
- **Pilot scope.** 30 RMs, English first (§2). Time to pilot matters more than channel breadth.

## Options considered

1. **CRM sidebar plus Teams personal tab** (both served by `web-bff` behind the internal Application Gateway; SSO
   via MSAL and Teams SSO), with a Teams bot via Azure Bot Service and the Microsoft 365 Agents SDK only after a
   residency sign-off.
2. **Teams bot first** (conversational, proactive messages such as "your 10:00 meeting brief is ready").
3. **Standalone web application** (a separate copilot site).
4. **Microsoft 365 Copilot extensibility**: a declarative agent with an API plugin calling `tool-gateway`, inside
   Microsoft 365 Copilot.

## Trade-off analysis

Scale: ++ strong, + adequate, 0 neutral, - weak, -- unacceptable.

| Criterion | 1 Sidebar + Teams tab | 2 Teams bot first | 3 Standalone web app | 4 M365 Copilot declarative agent |
|---|---|---|---|---|
| Residency | ++ traffic goes from the client straight to `web-bff` in UAE North | -- global Bot Framework connector in the message path (C1) | ++ | ? depends on Microsoft 365 Copilot in-country processing commitments for the Bank's tenant **[confirm]** |
| Private network posture | ++ internal ingress only | -- needs a messaging endpoint reachable by the Bot Framework service | ++ | - plugin endpoint must be reachable by the Microsoft 365 Copilot service |
| Verified release and rendering control | ++ our UI renders exactly the verified text | 0 Adaptive Cards are controllable, but message updates and chat rendering add constraints | ++ | -- Microsoft 365 Copilot's orchestrator writes the final answer from plugin output; our placeholder fill, numeric and policy checks no longer govern what the RM reads |
| Write approval UX and controls | ++ web form with CSRF, Origin, step-up | - approval in a chat card needs a separate secure flow | ++ | - approval confirmation is a platform prompt, not our signed flow |
| Workflow fit | ++ in the CRM record and in Teams | + conversational, proactive | - another tab and another URL; adoption risk | + inside the M365 surface RMs may already use |
| Identity chain (§3.4) | ++ MSAL and Teams SSO tokens with `web-bff` audience, then OBO | + Bot SSO is workable but more complex | ++ | 0 plugin auth via OAuth or Entra SSO; OBO chain feasible **[confirm]** |
| Licensing and dependency | ++ none beyond existing | + Bot Service | ++ | - Microsoft 365 Copilot licences for every RM |
| Time to pilot | + two hosts for one UI | - residency sign-off is on the critical path | ++ | - platform and licensing decisions on the critical path |

## Decision

Adopt **option 1**.

1. **One UI, two hosts.** A single web front end served by `web-bff` behind the internal Application Gateway,
   embedded as a CRM sidebar panel and as a Teams personal tab. Both render plain text with sources (C19), strict
   CSP set by `web-bff`, no external links or images.
2. **Authentication.** CRM sidebar: MSAL in the page, token audience `web-bff`. Teams tab: Teams SSO (`getAuthToken`)
   with the same audience. `web-bff` then performs OBO to `tool-gateway` (§3.4). The tab's domain is listed in the
   Teams app manifest; the app is published to the Bank's tenant catalogue only, assigned to pilot RMs.
3. **Reachability.** The tab loads from the internal hostname, so it works only on managed devices on the corporate
   network or through Entra Private Access. Teams mobile and off-network use are out of Phase 1 unless the
   device posture and Private Access configuration support them **[confirm Teams mobile behaviour with Private
   Access]**.
4. **Long-running requests.** The UI shows progress steps (ADR-022) through server-sent events or short polling
   from `web-bff`; no chat-style streaming of model text.
5. **Email.** The approved email opens in Outlook via a compose deeplink; the copilot never writes to the mailbox
   (C20).
6. **Phase 2 bot.** `bot-adapter` (Azure Bot Service with the Microsoft 365 Agents SDK) is built only after the
   Bank signs off residency for bot traffic (C1). The bot is a thin channel adapter that calls `web-bff`; it
   inherits verified release, and writes still require the web approval flow (the bot links to it).
7. **Microsoft 365 Copilot extensibility is not used** for these use cases while it would re-generate our verified
   output. It may be reconsidered for read-only, low-risk scenarios (for example a "find the policy document"
   action that returns links to approved documents without generated wording).

## Consequences

Positive

- No global message relay in the data path; the residency story is the same as the rest of the platform.
- The approve flow keeps its web security controls (CSRF, Origin, step-up) without inventing a chat equivalent.
- The copilot appears inside the CRM record, which is where UC1 and UC3 start.

Negative

- No proactive notifications in Phase 1 ("your brief is ready"); RMs must open the tab or sidebar. Brief
  prefetch (C9) is a Phase 2 item anyway.
- CRM sidebar embedding depends on the CRM product's extension model (iframe hosting, CSP `frame-ancestors`,
  token acquisition inside the CRM) **[confirm with the CRM team]**.
- Two host contexts to test (CRM web client, Teams desktop and web clients), each with its own SSO quirks.
- Off-network and mobile use are limited by the private ingress choice.

Risks and mitigations

| Risk | Mitigation |
|---|---|
| CRM blocks third-party iframes or third-party cookies | No cookies needed for auth (bearer tokens); CRM extension model validated in discovery; fallback: CRM deeplink that opens the Teams tab with the customer context |
| Teams SSO consent friction | Admin consent for the tenant; pre-authorised Teams client IDs on the `web-bff` app registration |
| RMs expect a chat experience | UC2 offers a question box in the tab; UX research in pilot decides whether a bot is worth the residency work |
| Residency sign-off never comes | Bot stays out of scope; tab remains the Teams channel |

## Review trigger

- Microsoft confirms in-country processing for Bot Framework traffic, or the Bank's residency policy changes
  (re-assess the bot).
- Microsoft 365 Copilot offers a mode in which plugin output is displayed verbatim, with in-country processing
  (re-assess option 4).
- Pilot research shows significant demand for mobile or off-network use.

## Related

- Plan: §2 (Phase 1 channels), §3.2 (`web-bff`, `bot-adapter`), §3.3 (Edge, M365 rows), §3.4 step 1, §4.1 steps
  1 and 10, §6 C1, C9, C18, C19, C20, §10 item 3.
- ADR-009 (approve flow), ADR-017 (internal ingress and Private Access), ADR-022 (progress steps instead of
  streaming). ADR-008 (identity chain, OBO,
  `Calendars.ReadBasic`), ADR-001 (residency).
