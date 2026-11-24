# RittaiHarness

RittaiHarness is a single-user, local-first SDLC workspace. Give the Lead Agent a software request, follow every agent attempt and review, and inspect the merged source and verified build. The workspace uses Python’s standard library, SQLite, Git, and Docker. The dashboard binds to loopback.

## Start

Install Python 3.12+, Git, Make, Node.js for JavaScript checks, and a running local Docker Engine or Docker Desktop. Run these commands from the checkout.

```sh
make all
make test
make doctor
make run
```

Open [the local workspace](http://127.0.0.1:8765/), create a project, and use **Run demo** to exercise the six-stage factory on a workshop task board. The demo needs the built images and Docker. Its fixed model responses and example research are bundled.

Use **Provider settings** to connect Microsoft Foundry or another service with a Chat Completions-compatible endpoint. For Foundry, paste the resource or project endpoint from the portal and enter the deployment name. RittaiHarness adds `/openai/v1` when needed. Use a resource key with a resource endpoint. For a project endpoint, use the project API key or a current Microsoft Entra bearer token. Compatible local servers can use a loopback HTTP URL. Other providers use HTTPS. The Lead sends the relevant prompt, source and handoffs to the selected provider.

```sh
export OMNI_PROVIDER=foundry
export OMNI_ENDPOINT=https://YOUR-RESOURCE.services.ai.azure.com/openai/v1
export OMNI_MODEL=YOUR-DEPLOYMENT
# Supply OMNI_API_KEY using your shell's secret management.
./dist/rittaiharness.pyz serve --data .omni-data --port 8765
```

`make build` produces `dist/rittaiharness.pyz` and `dist/rittaiharness-src.tar.gz`. Run the zip application with `python3 dist/rittaiharness.pyz`. The source archive contains everything needed to rebuild its three Docker images. The first image build downloads base layers and Playwright dependencies. `make smoke` runs the demo against real containers, verifies all seven approvals including the Lead plan, fetches the preview, and checks the ZIP.

## Run from the terminal

`make all` builds the executable and the required Docker images. Run a complete demo without opening the dashboard.

```sh
./dist/rittaiharness.pyz doctor
./dist/rittaiharness.pyz run --demo
```

For a custom request, configure the provider in the dashboard or set `OMNI_PROVIDER`, `OMNI_ENDPOINT`, `OMNI_MODEL`, and any needed `OMNI_API_KEY` in your shell. The CLI accepts a project name or ID. It creates the named project if it does not exist. Pass a quoted request, use `--prompt-file request.md`, or read a request from stdin with `--prompt-file -`.

```sh
./dist/rittaiharness.pyz run --project "Task board" "Add keyboard shortcuts to create and complete tasks."
./dist/rittaiharness.pyz sessions --project "Task board"
./dist/rittaiharness.pyz logs SESSION_ID --stage qa --follow
./dist/rittaiharness.pyz status SESSION_ID --json
./dist/rittaiharness.pyz retry SESSION_ID
./dist/rittaiharness.pyz cancel SESSION_ID
./dist/rittaiharness.pyz preview SESSION_ID
```

The CLI uses the running dashboard for the same workspace when available. Use `--connect PORT` to choose a specific loopback dashboard, or `--data PATH` to use a separate workspace. For a standalone run, the CLI hosts the Lead until the run completes and preserves the build ZIP. Run `preview` to keep the completed application available while the command is running. When connected to a dashboard, use `run --detach` or `retry --detach` to queue a run and return its ID. Use `serve` to open the dashboard on demand, or `--help` to list the CLI commands. Failed runs return a nonzero exit status.

## Model discovery and local credentials

In **Provider settings**, choose **Microsoft Foundry**, enter the project endpoint ending in `/api/projects/<project>` and its credential, then select **Load deployments**. RittaiHarness lists the project’s model deployments. Selecting one fills in the deployment name sent with chat requests. If you use a resource-only endpoint, enter your deployment name directly. Gateways and compatible servers can use **Load models** when they expose a `/models` route. [Foundry project deployments API](https://learn.microsoft.com/en-us/rest/api/microsoft-foundry/aiproject#deployments---list)

**Remember in the system keyring** saves the provider credential in the Linux desktop Secret Service, such as GNOME Keyring or KWallet with Secret Service enabled. RittaiHarness keeps only a reference in its workspace database. A saved credential is scoped to the workspace and provider endpoint and is shared by the dashboard and CLI. Leaving **Remember** unchecked keeps a newly entered credential in the dashboard process. Use **Forget saved credential** and save settings to remove the keyring entry. Entra access tokens still expire and must be refreshed. On Debian and Ubuntu, the runtime bindings are provided by `python3-gi` and `gir1.2-secret-1`. The desktop keyring must be reachable from the process.

```sh
./dist/rittaiharness.pyz models --json
./dist/rittaiharness.pyz credential save
./dist/rittaiharness.pyz credential forget
```

For a Foundry project, `models` prints deployment names. `credential save` prompts with input hidden. Use `credential save --from-env` to save an already loaded `OMNI_API_KEY` or `AI_GATEWAY_API_KEY`. You can also set `OMNI_PROVIDER` and `OMNI_ENDPOINT` to discover deployments or save a credential before configuring a deployment.

## Microsoft Foundry through Azure AI Gateway

Use the Azure AI Gateway resource described in Microsoft's overview. Import a chat-capable Foundry model into the gateway, then copy its full Chat Completions endpoint and callable alias from the gateway portal. Use an Azure AI Gateway runtime key. The Lead sends it as a bearer credential and the gateway handles authentication to Foundry. [Azure AI Gateway overview](https://learn.microsoft.com/en-us/azure/ai-gateway/overview) · [Microsoft quickstart](https://learn.microsoft.com/en-us/azure/ai-gateway/quickstart-create-ai-gateway)

In the dashboard, select **Azure AI Gateway for Foundry** under Provider settings, then enter the endpoint, alias, and runtime key. **Test saved provider** sends a fixed connectivity message. From a terminal, use the copied values and load the runtime key into `AI_GATEWAY_API_KEY` through your shell's secret management or save it in the system keyring.

```sh
export OMNI_PROVIDER=azure-gateway
export AI_GATEWAY_CHAT_URL='https://YOUR-GATEWAY-HOST/default/models/openai/v1/chat/completions'
export AI_GATEWAY_MODEL='YOUR-CALLABLE-ALIAS'
./dist/rittaiharness.pyz provider-test
./dist/rittaiharness.pyz run --project "Task board" "Build a task board with accessible keyboard navigation."
```

The gateway endpoint may also be supplied as `OMNI_ENDPOINT`, the alias as `OMNI_MODEL`, and the runtime key as `OMNI_API_KEY`. The Lead redacts known credentials from telemetry.

## MCP and Skills

Select the extensions each role can use in `extensions.toml` inside your workspace data directory. The dashboard **Extensions** view and `extensions --check` list configured scopes and verify MCP discovery. The Lead reads extension configuration when a new session starts. The bundled demo continues to use its fixed offline inputs.

For a first workspace configuration, copy the shipped example and its Skill into the data directory.

```sh
mkdir -p .omni-data
cp -R examples/skills .omni-data/
cp -n examples/extensions.toml .omni-data/extensions.toml
./dist/rittaiharness.pyz extensions --check
```

The example grants the Research role the public Microsoft Learn search and fetch tools and assigns the lean-code Skill to Architecture, Development, and Review. You can also set `OMNI_EXTENSIONS` to an explicit configuration path when starting the Lead. Skill and stdio working-directory paths resolve relative to the configuration. [Microsoft Learn MCP documentation](https://learn.microsoft.com/en-us/training/support/mcp-developer-reference)

- **MCP** accepts Streamable HTTP and trusted local stdio servers. A tool must be listed for the role in TOML and advertise `readOnlyHint: true` before the Lead can call it. The Lead validates tool names and argument sizes, redacts known secrets, bounds tool output, and logs calls and results. MCP results enter the model as untrusted evidence. Local stdio commands are owner-configured executables that run as the workspace user.
- **Gateway-published MCP** uses the copied MCP endpoint of your federated Azure AI Gateway tool server as its `url`. Set `token_env` to the environment variable containing its bearer credential. For endpoints using key-based headers, set `api_key_env` and optionally `key_header` to `api-key`, `x-api-key`, or `Ocp-Apim-Subscription-Key`. Model and MCP endpoints are configured independently. [Azure AI Gateway concepts](https://learn.microsoft.com/en-us/azure/ai-gateway/concepts)
- **Skills** load trusted `SKILL.md` guidance and explicitly selected text resources for the named roles. Resources must reside within the selected Skill directory. A Skill's instructions can use the role's existing tools. Script execution and additional permissions come from the harness, independently of the Skill. [Agent Skills format](https://agentskills.io/specification)

## Role container tools

The Lead calls the configured model and brokers approved network access. In each role container, an offline tool accepts bounded JSON over a pipe, checks its arguments, and returns structured evidence. The Lead records tool calls and results with the attempt. Roles have an allowlist of operations for their assignment.

| Role | Container tools and output |
| --- | --- |
| Lead | Builds the station assignment and acceptance criterion matrix. |
| Research | Indexes citations from the Lead’s verified public sources. |
| Architecture | Checks the proposed runtime and deployment mode against the runtime matrix. |
| UX/UI design | Renders escaped, standalone HTML from the draft wireframes and records accessibility requirements. |
| Development | Uses Python AST parsing and Node syntax checks on draft code, then reports imports, file sizes, checksums, and identical files. |
| QA/UAT | Produces an acceptance-criteria coverage matrix. The QA runner executes language tests and Chromium journeys. |
| Release | Produces a checksum manifest for the verified source. The runtime rebuilds it and hosts the loopback preview. |
| Independent QA | Checks that the required execution evidence is present before the reviewer model sees the deliverable. |

The worker image provides the Python tools. Development and release use the runtime image with Python and Node. The QA image includes Chromium and Playwright. All images run with the existing network, mount, and process restrictions.

## Factory stages and approvals

1. The Lead defines six assignments and observable acceptance criteria. A separate QA reviewer checks the plan.
2. Research iterates over evidence requests, checks official documentation on allowlisted hosts, and can query OSV for proposed package versions. The next stage starts only when an independent reviewer approves the research.
3. Architecture and UX each create structured, separately reviewed handoffs.
4. Development emits the complete source tree. The Lead checks the path and runtime contract, scans locked dependency versions against OSV, builds in Docker, and routes source and evidence for review.
5. QA/UAT associates each acceptance criterion with a check, runs applicable language tests, and runs Playwright Chromium journeys for web deliverables. Independent review decides the gate.
6. Release rebuilds the tested source and reviews the verification handoff. The Lead packages the source into a ZIP, checks the isolated web preview if applicable, and atomically merges the session branch into `main`.

The Lead records every attempt with its persona, input, resource allocation, tool events, log output, and exceptions. Gate failures produce audit commits, include the failure context in a replacement attempt, and stop the factory after three failures. A failed or cancelled session can resume at its first unapproved stage. On restart, the Lead recovers approvals from Git, marks interrupted attempts, and removes leftover workspace containers when Docker is available. One session at a time changes a project branch. Sessions for different projects can progress concurrently.

The workspace controls local project creation, provider settings, cost estimates and Hive Memory. Project vitals show sessions, tokens, model and compute estimates, agent counts, and build health. Each session has a floating Lead chat and a combined Team & Factory Line view, alongside Summary and Live Output. The reporting hierarchy and stage map update every 1.8 seconds. A touch is one agent attempt, including a retry. Select a role, station, or touch count to filter the shared execution log. Drill into an individual attempt to inspect its exact persona, input, resource profile, and trace. Changes to requirements start a follow-up session with its own approvals.

## Runtime and data

- **Deliverables** use a `harness.json` with `runtime` set to `static`, `python`, or `node` and `mode` set to `web` or `artifact`. Python and Node also specify a source `entry` and runnable unit tests. Web apps serve on `HOST=127.0.0.1` and `PORT=8080` inside the runtime. Artifact entry points run without arguments during build verification. Source is text, with a 120-file and 1.25 MB limit. App code runs directly in Chromium, Python, or Node without arbitrary build scripts.
- **Dependencies** default to the runtimes’ standard libraries. Python can use exact pins for all dependencies in `requirements.txt` and a trusted offline wheel directory set through `OMNI_WHEELHOUSE`. Node can use an npm v2/v3 lockfile with integrity fields and an offline cache set through `OMNI_NPM_CACHE`. Installers run offline and npm lifecycle scripts are disabled. The Lead checks known OSV advisories before executing builds.
- **Sandboxes** use separate role, QA, and runtime resource profiles, a non-root user, no Linux capabilities, read-only root filesystems, bounded scratch filesystems, and no direct network. The Lead brokers configured model traffic, public HTTPS research on exact approved hostnames, and OSV checks. `OMNI_RESEARCH_HOSTS` adds comma-separated official hosts. The Lead filters suspicious instructions and redacts common credentials and configured provider keys from persisted telemetry.
- **Previews** run in a dedicated networkless container. A loopback HTTP relay forwards to the application inside that container. Preview lifetime defaults to one hour and can be changed with `OMNI_PREVIEW_SECONDS`. Start or stop previews from Live Output. Preview URLs use a separate origin and are refreshed on restart. Previews support ordinary HTTP requests.
- **Git** lives under `.omni-data/monorepo`. Source lives at `projects/<project-id>/app`. Session documents and audit logs live at `.omni/sessions/<session-id>`. Session worktrees, packages and staging inputs share the same private data directory.
- **Hive Memory** stores nodes and edges in the SQLite workspace database. Approved decisions, verified patterns, failures and remedies persist across projects. Relevance increases when an approved artifact demonstrates use of a retrieved node. Offline Dreaming deduplicates canonical records, prunes old unlinked low-weight nodes, and connects recurring fixes and cross-project practices. Set `OMNI_DREAM_INTERVAL` to tune its interval or use the workspace button during idle time.
- **Costs** use provider-reported input and output tokens and estimated CPU allocation time. Set rates in Provider settings to display nonzero estimates.
- **Timeouts** are bounded per stage attempt with `OMNI_STAGE_TIMEOUT`, defaulting to 240 seconds. Model calls, source retrieval, browser actions, execution logs, process counts and preview requests also have narrower limits.

The workspace directory is per OS user. Store it on local storage accessible to the local Docker daemon. This implementation supports a single workspace owner on loopback. The preview is a local release environment.


<details>
<summary>Agent portrait generation prompt</summary>

The portraits were generated with the built-in ImageGen tool and are packaged in `omni/web/agent-portraits.png`.

> Create one production sprite-sheet raster asset for the RittaiHarness AI operations dashboard. Precisely 4 equal columns by 2 equal rows of square portrait tiles, edge-to-edge, zero gutters, divisions exactly at 25/50/75 percent of width and 50 percent of height. Every tile is a different sleek, expressive ceramic-and-metal robot agent bust, front/three-quarter view, with the face centered in that tile and generous margins inside its tile, instantly readable when cropped to a 40px square. Art direction: polished stylized 3D editorial product illustration, dark midnight navy studio backgrounds, sculptural shadows, modest restrained color accents and lovely distinct silhouettes. Top row left to right: (1) Lead orchestrator, mint ring detail on ivory and graphite face; (2) Research, cyan dual optical lenses on navy shell; (3) Architecture, violet angular geometric ivory face; (4) UX design, coral and rose smooth expressive helmet. Bottom row left to right: (5) Developer, amber visor on dark brushed alloy; (6) QA, sky-blue binocular precision sensors on silver; (7) Release, jade wings or streamlined shell on charcoal; (8) Independent QA reviewer, lavender single aperture and compact charcoal shell. Consistent styling and scale across all eight tiles. Each portrait stays inside its exact cell. No frames, text, letters, numbers or watermarks. Wide landscape, ideally 2:1 ratio.

</details>
