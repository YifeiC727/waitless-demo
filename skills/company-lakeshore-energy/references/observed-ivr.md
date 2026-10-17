# Team-observed ComEd IVR

Observed by the team on October 17, 2026. This is evidence from one call, not a promise that ComEd will keep the same menu. Always prefer the complete prompt heard on the current call.

## Main menu

- Spanish: press 8.
- Electrical issues or power outages: press 1.
- Pay now: press 2.
- Everything else: press 3.

Start, stop, and move service were not named in this prompt. If this exact complete main menu is heard during a service request, `everything else` is the only offered category that fits; press 3. Do not infer any later key until its prompt is heard.

## Residential start-service branch observed after pressing 3

After `everything else` was selected, the system said the office was closed and requested account lookup:

- Account number: press 1.
- Phone number: press 2.
- Social Security or Tax ID: press 3.
- Set up a new account: press 4.

For a new residential start-service request on this exact prompt, press 4. The next prompt asked where service should start and accepted the spoken choices `My home` or `My business`; the observed residential choice was `My home`.

The system then offered to text a Start Service web-app link to the calling number. It asked first whether the caller wanted to use the web app and then separately asked for permission to send the text. After both were answered yes and receipt was confirmed with `I got it`, the system ended the voice connection and directed the caller to continue on screen.

Do not accept this handoff automatically. A cloud phone agent may be calling from a service number that cannot deliver the link to the account holder. Accept only when the task runtime can securely receive the SMS and make the web flow available to the user; otherwise decline and continue listening for a voice or representative path. Consent to use the web app does not by itself authorize sending a text.

In a second team-observed call made while the office was closed, the caller declined the web app. The system did not offer a voice alternative or representative; it ended the flow and instructed the caller to call during its stated normal business hours, Monday through Friday, 7 a.m. to 7 p.m. Central Standard Time. Therefore, outside staffed hours, declining the web app is a known dead end. If secure SMS relay is unavailable, schedule or retry the call during staffed hours instead of promising that the automated voice flow can complete the request.

## Outage branch observed after pressing 1

- Total loss of power or outage update: press 1.
- Damaged power lines or equipment: press 2.
- Partial loss of power, dim lights, or flickering lights: press 3.
- Service shut off for nonpayment: press 4.
- Streetlight problems: press 5.
- Cover power lines during work or voltage information: press 6.
- Main menu: press pound (`#`).

This is an outage branch. Do not use it as evidence for start, stop, or move service navigation.

## Account lookup varies by route

The observed calls contained two different account-lookup prompts:

1. Account number: 1; phone number: 2; Social Security or Tax ID: 3; set up a new account: 4.
2. Phone number: 1; account number: 2; Social Security or Tax ID: 3.

Never cache a field-to-digit mapping across menus. Wait for the current prompt to finish, then choose the key attached to the authorized field in that prompt. Social Security and Tax ID values are private verification data and must not be supplied by the AI.

At the second account-lookup prompt, the caller pressed star (`*`) and the system transferred the call. Treat this as one observed result, not a general rule that star always reaches a representative.

The transfer announcement said the office was closed, automated service remained available, and calls were recorded. This observation does not establish normal staffed-hours routing.
