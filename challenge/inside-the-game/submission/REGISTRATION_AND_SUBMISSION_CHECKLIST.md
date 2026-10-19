# Registration and submission checklist

## Tuesday, October 20, 2026 - registration

Official registration closes at **12:00 PM Pacific / 3:00 PM Eastern (Washington, DC)**. Do not wait until evening.

- [ ] Visit `https://aka.ms/insidethegame` and click **Register**.
- [ ] Confirm individual, team (maximum four individuals), or organization entry.
- [ ] If entering as a team/organization, name one authorized representative.
- [ ] Confirm every participant meets the eligibility rules and has no conflict.
- [ ] Save the confirmation email/screenshot outside the public repository.
- [ ] Use project title: **MatchPulse Live**.
- [ ] Use `submission/PITCH.md` as the working elevator pitch.
- [ ] Select target categories only after the implemented/deployed proof supports them.

## October 20-24 - make the proof real

- [ ] Add this package to a new challenge branch or clean public repository.
- [ ] Run tests and preflight on Windows and in GitHub Actions.
- [ ] Create a Microsoft Foundry project/model deployment and configure the guarded adapter.
- [ ] Deploy the container to Azure Container Apps.
- [ ] Confirm the public demo has no login restriction and remains free to test.
- [ ] Add an on-page synthetic feed trigger if judges will not run a CLI.
- [ ] Measure and record deterministic and Foundry-path latency.
- [ ] Capture Application Insights requests/errors and the active Azure revision.

## October 25-26 - submission package

- [ ] Make the GitHub repository public.
- [ ] Verify no secrets, `.env`, private logs, real player data, or unrelated files exist in history.
- [ ] Confirm the final repository license and ownership.
- [ ] Record the demo from the exact public commit.
- [ ] Keep the final video <= 1:55 and add English captions.
- [ ] Upload to YouTube, Vimeo, Facebook Video, or Youku with a public URL.
- [ ] Add final public repository, demo, and video URLs to the project description/testing instructions.
- [ ] Run `python scripts/preflight.py` and archive the output.

## Tuesday, October 27 - submit

The official submission period ends at **11:59 PM Pacific**, which is **2:59 AM Eastern on October 28**. Use an internal deadline of **11:59 PM Eastern on October 27** so the project is visibly submitted on the user's requested date.

- [ ] Complete every required field on the Projects page.
- [ ] Paste the pitch and description; name Microsoft/Azure technologies actually demonstrated.
- [ ] Add the public GitHub URL.
- [ ] Add the public video URL.
- [ ] Add the hosted demo/test-build URL and concise testing instructions.
- [ ] Confirm all submission materials are in English (multilingual product output is fine).
- [ ] Confirm the project is newly built, original, and has not won another contest.
- [ ] Recheck video for third-party marks, copyrighted music, and private data.
- [ ] Submit, reopen the project page, and verify all links from a private browser window.
- [ ] Save the confirmation and final URLs.

## Final category honesty check

- Claim **Best Multi-Agent System**: yes, if the shown build matches this code.
- Claim **Best Azure Cloud Native Integration**: only after the Azure deployment and telemetry are live.
- Claim **Best Use of Microsoft Foundry**: only after a real Foundry call is shown or evidenced.
- Claim **Best Enterprise Solution**: describe current controls and disclose in-memory/MVP limits.
