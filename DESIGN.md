# Waitless interface decisions

## Current all-surface rules

Task/outcome first; one dominant action; supporting information secondary. No consumer integration boilerplate. Profile editing, approval and expandable history live in chat; only the phone changes interaction mode. Current progress replaces earlier progress. Demo callbacks advance automatically, without extra confirmation. These decisions supersede older setup-sheet and ticket descriptions below; see UX-REVIEW.md.

## Phone handoff

Keep the handoff prompt in chat: the representative is connected, no new queue, and AI will not listen during verification. Join demo call loads the handoff tool and enters the private call directly. Remove the redundant full-page ticket and second Answer action. Consumer headers have no MCP pills; technical trace is accessible only via debug=1.

## Private call

Minimal consumer call UI: provider name, elapsed time, Mute/Keypad/Speaker, Return to Waitless and a single small DEMO marker. No Private call, service-representative subtitle, Only you message, verification instructions or extra help button. Privacy context belongs in the preceding chat handoff card. Identity occupies flexible upper space, controls sit lower, and the primary return action anchors the bottom.

The call view fits the available screen height without scrolling. Flexible identity space, compact short-screen spacing, and a landscape arrangement keep controls and Return to Waitless visible. Opening the keypad replaces the identity section rather than increasing page height. Removed the redundant Simulated audio footer; the top DEMO marker still identifies simulation.

Use a calm navy surface, mint primary action, consistent outline SVG icons, a real elapsed timer, and accessible mute/speaker toggles. The keypad is an expandable demo control. The primary action is “Return to Waitless”; no implementation details about MCP or conference infrastructure appear in the consumer screen.

The phone view is a simulated companion representation. A production system-phone handoff uses the operating system's call UI; these web controls do not control that phone call.

## Confirmation decisions

Check the saved sample profile during loading and show one explicit pre-call card. The details sheet is optional editing, not a required step. Preserve one explicit choice before cancellation because immediate cancellation and stopping auto-renewal differ. No choice is preselected. After private verification, confirmation progresses automatically. The result distinguishes a requested refund from a received payment and follows the selected branch.

## Guidance

Reviewed UI UX Pro Max skill from https://github.com/nextlevelbuilder/ui-ux-pro-max-skill. Applied its guidance on information hierarchy, 44px-or-larger touch controls, consistent vector icons, contextual loading, focus visibility, reduced motion, and responsive layout. Existing Alexa-inspired visual direction and standalone HTML implementation are retained.
