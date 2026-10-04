# Set up my Project Observatory

Help me install and configure the full Project Observatory on this computer.
Read ONBOARDING.md and COMPATIBILITY.md first: under `docs/` in a checkout of the release, or under `$(project-observatory full-path)/docs/` in an installed release.

1. Verify Python and SQLite requirements. Explain the code directory and the separate private workspace. Run `project-observatory full version`, then `full init` and `full doctor` with an explicit OBSERVATORY_HOME.
2. Ask which project directory I want to observe. Configure only that directory. Explain which local metadata will be read. Start with filesystem scanning and the local dashboard.
3. Offer optional GitHub, Bitbucket, hosting, DNS, analytics, memory and model integrations separately. Missing integrations remain unconfigured. Do not enable paid reasoning, remote secret reads, memory remediation, notifications or scheduled jobs as part of a default setup. Remote memory embeddings need my own consent per project (`full embedding-policy grant`, at my terminal); you cannot give it, and your searches stay local either way. Say this before I enable embeddings. Any caller other than you (the local stdio agent) reaches memory only through a binding I issue at my terminal (`full access-binding issue`); you cannot issue one, and I hand its bearer over as a file, never in chat.
4. Explain the minimum credential permissions for each selected integration. Have me enter secrets in a local hidden prompt, secure file or credential manager. Never ask me to paste a value into this conversation. Report only names and connection results.
5. Run a first observation and show its coverage, findings and unavailable inputs. A disconnected source is not a clean security finding.
6. Explain how to update, preview a migration and restore a backup. If this is an existing installation, stop writers before migration and keep the original until the new copy is verified. Do not start a second scheduler over the same data.
7. Finish with the local dashboard address, commands for another scan, and the list of enabled integrations. Keep the public marketing site separate from the private dashboard.
