# Architecture diagrams

Diagrams and walkthrough of `virt-runner`'s module layout, class hierarchy, and
the main logic flow of each subcommand. Generated from the source in
`src/virt_runner/` (see the code — it is the source of truth).

## 1. Module / component architecture

```mermaid
flowchart TB
    subgraph CLI["CLI layer (click)"]
        MAIN["main() — click.group<br/>cli.py"]
        JC["JsonCommand — click.Command subclass<br/>args.py: injects --json, JSON-ifies usage errors (exit 2)"]
        CC["cmd_create"]
        CD["cmd_destroy"]
        CL["cmd_list"]
        CS["cmd_ssh"]
    end

    subgraph CORE["Core (no click beyond decoration)"]
        V["Virtualizer<br/>virtualizer.py — every libvirt/subprocess op<br/>helpers: _run _stdout _spawn _quiet _passthrough"]
        IMG["fetch_and_verify_image<br/>images.py — glob → curl → sha256 → cache"]
        PROF["PROFILES: dict[str, DistroProfile]<br/>profiles.py — data-driven ubuntu/arch/fedora"]
    end

    subgraph X["Cross-cutting"]
        OUT["output.py — module-level JSON mode flag<br/>progress/warn (silent under --json), emit/fail envelope"]
        ERR["errors.py — VirtError(code, stage)"]
    end

    subgraph HOST["Host tools (subprocess — no libvirt python bindings)"]
        T1["virsh"]
        T2["virt-install"]
        T3["ssh / ssh-keygen"]
        T4["curl"]
        T5["dnsmasq lease file (virbr0.status)"]
    end

    MAIN --> CC & CD & CL & CS
    CC & CD & CL & CS -. "built via @command() (cls=JsonCommand)" .-> JC
    CC --> V & IMG & PROF
    CD --> V
    CL --> V
    CS --> V
    V --> T1 & T2 & T3 & T5
    IMG --> T4
    CC --> OUT
    CD --> OUT
    CL --> OUT
    CS --> OUT
    V --> ERR
    IMG --> ERR
    OUT --> ERR
```

**Design in one paragraph:** two strict layers. The `cmd_*.py` files are thin
CLI adapters: argument validation (click callbacks), pipeline orchestration via
a `stage` variable, and rendering. Everything that touches the host lives in
`Virtualizer` (subprocess-only; every call goes through one of five helpers so
error semantics — raise / capture / ignore / passthrough — are explicit at the
call site) and `images.py`. `output.py` is a tiny module-level state machine:
one bool decides whether stdout carries human text or exactly one JSON
envelope; `--json` suppresses all progress output so
`virt-runner create x --json | jq .` is always clean. Errors are `VirtError`
(a `RuntimeError` carrying a stable `code` + pipeline `stage`), which the CLI
layer converts into the JSON error envelope or a `vm-<cmd>: message` stderr
line. Exit codes never depend on output mode: 0 success, 1 runtime/preflight,
2 usage.

## 2. Class hierarchy

```mermaid
classDiagram
    class click__Command {
        +get_params(ctx)
        +make_context(info_name, args, parent)
        +invoke(ctx)
    }

    class JsonCommand {
        +get_params(ctx)
        +make_context(info_name, args, parent)
    }
    note for JsonCommand "get_params: appends the shared --json option. make_context: pre-scans raw args for --json BEFORE parsing, so usage errors are reported as JSON (exit 2) when requested."

    class cmd_create {
        <<JsonCommand instance>>
        pipeline: preflight → image → cloud-init → create → wait-ip → ssh-verify
    }
    class cmd_destroy {
        <<JsonCommand instance>>
    }
    class cmd_list {
        <<JsonCommand instance>>
    }
    class cmd_ssh {
        <<JsonCommand instance>>
    }

    class Virtualizer {
        POOL$ : "vm-pool"
        NET$ : "default"
        METADATA_URI$
        DEFAULT_USER$ : "ubuntu"
        DEFAULT_SSH_KEY$
        DEFAULT_LEASE_FILE$
        +preflight_check()
        +require_libvirt()
        +require_pool_defined()
        +require_domain_absent(name)
        +require_virtiofsd()
        +ensure_ssh_key(path)
        +host_arch()
        +generate_cloud_init_files(...)
        +provision_volume(name, disk_gib, image_path)
        +create_vm(...)
        +set_domain_metadata(name, distro, user, identity)
        +domain_meta(name)
        +wait_for_ip(name, mac, lease_file)
        +verify_ssh_reachable(name, user, ip, identity)
        +vm_ip(name, lease_file)
        +ssh_shell(user, ip, identity)
        +list_vms(lease_file)
        +destroy_vm(name)
    }

    class VmInfo {
        <<frozen dataclass>>
        +name
        +uuid
        +state
        +mac
        +ip
        +ip_status
        +meta : dict
        +running() bool
    }

    class TeardownResult {
        <<frozen dataclass>>
        +was_running
        +volume_deleted
        +volume_already_absent
        +confirmation
        +warning
    }

    class ImageFetch {
        <<frozen dataclass>>
        +path
        +cache_hit
        +downloaded
        +verification
        +verified
    }

    class DistroProfile {
        <<frozen dataclass>>
        +name
        +default_release
        +suggested_user
        +sudo_group
        +os_variant
        +image_url : Callable
        +sums_url : Callable
        +cache_prefix
        +releases : tuple|None
    }

    class RuntimeError
    class VirtError {
        +code : str
        +stage : str|None
    }

    click__Command <|-- JsonCommand
    RuntimeError <|-- VirtError
    JsonCommand --> cmd_create : instance
    JsonCommand --> cmd_destroy : instance
    JsonCommand --> cmd_list : instance
    JsonCommand --> cmd_ssh : instance
    Virtualizer ..> VmInfo : list_vms() returns
    Virtualizer ..> TeardownResult : destroy_vm() returns
    Virtualizer ..> VirtError : raises
    DistroProfile ..> ImageFetch : via fetch_and_verify_image()
```

Notes:

- The four subcommands are **instances** of `JsonCommand`, not subclasses —
  `args.command()` is `click.command()` with `cls=JsonCommand` as default, so
  `@command(name="create")` yields a `JsonCommand` object. The only true
  subclassing in the package is `JsonCommand → click.Command`.
- `Virtualizer` is a flat class with no inheritance; its public methods mirror
  the pipeline steps of the original bash scripts.
- The four dataclasses are pure fact records: `VmInfo`/`TeardownResult`
  (rendered verbatim by `cmd_list`/`cmd_destroy`), `ImageFetch` (rendered by
  the `create` document), `DistroProfile` (data-driven config — the pipeline
  code has no distro-name conditionals).
- `VirtError` extends `RuntimeError` on purpose: existing
  `except RuntimeError` handlers keep working, while the CLI layer reads
  `.code`/`.stage` for the JSON envelope instead of string-matching messages.

## 3. Flow of the subcommands

### 3.1 `create` — the full pipeline

```mermaid
flowchart TD
    A["virt-runner create NAME [options] [--json]"] --> B{"JsonCommand.make_context"}
    B -->|parse failure| C{"--json in raw args?"}
    C -->|yes| C1["JSON error envelope, code=usage, exit 2"]
    C -->|no| C2["click usage error, exit 2"]
    B -->|parsed| F["set_json_mode(as_json)<br/>v = Virtualizer(); profile = PROFILES[distro]"]
    F --> G["D1 precedence: explicit --release wins over --image<br/>resolve image_url, effective_release, effective_user<br/>build run_state dict (accumulated through the run)"]

    G --> H["stage := preflight"]
    H --> H1{"x86_64? · virsh list ok?<br/>pool vm-pool active?<br/>net default active?<br/>domain absent?<br/>virtiofsd present (if --mount)?"}
    H1 -->|fail| X
    H1 -->|pass| H2["ensure_ssh_key: reuse / derive via ssh-keygen -y /<br/>generate ed25519 keypair (no partial key left behind)"]

    H2 --> I["stage := image"]
    I --> I1["fetch_and_verify_image (images.py):<br/>cache dir ~/vm-images[/prefix/release] (VM_CREATE_CACHE_DIR)<br/>glob resolve (Fedora rotating names, newest first, cache fallback)<br/>cache hit? → ImageFetch(verification from .verified marker)<br/>else curl -fL --retry 3 → sha256 vs own SHA256SUMS entry<br/>(.verified marker written; no entry or mismatch = fail)"]
    I1 -->|ImageFetch| J["stage := cloud-init"]
    I1 -->|VirtError| X

    J --> J1["render_user_data / render_meta_data (pure fns)<br/>user: sudo + ssh_authorized_keys; bootcmd: virtiofs mounts<br/>temp dir kept for virt-install, removed via atexit"]
    J1 --> K["stage := create"]

    K --> K1["provision_volume:<br/>virsh vol-create-as (qcow2) → vol-upload → vol-resize<br/>(PI-1: vol-upload silently resizes; cleanup vol on any failure)"]
    K1 --> K2["generate_mac: 52:54:00:<random 3 bytes>"]
    K2 --> K3["create_vm: virt-install --import --cloud-init ...<br/>via _passthrough (user sees its own output;<br/>under --json stdout steered to stderr)<br/>on failure: destroy + undefine + vol-delete — nothing left behind<br/>--memorybacking shared + --filesystem virtiofs per --mount"]
    K3 --> K4["run_state: created, domain_state, uuid<br/>set_domain_metadata (distro/user/identity; warn-only on failure)"]
    K4 --> L{"--no-boot?"}

    L -->|yes| M["stop domain if running; state := shut off"]
    L -->|no| N["stage := wait-ip<br/>poll dnsmasq lease file (JSON, legacy fallback), 120 s @ 2 s<br/>lease accepted only while domain is running<br/>fallbacks ONLY if lease file unusable: domifaddr (30 s), arp -n"]
    N --> O["stage := ssh-verify<br/>poll ssh BatchMode probe (injected identity, known_hosts=/dev/null), 90 s"]

    M --> P
    O --> P
    P{"done without error?"}

    P -->|RuntimeError at any stage| X["output.fail_with: code from VirtError else stage default<br/>(preflight→libvirt-unreachable, image→image-download-failed,<br/>cloud-init→cloud-init-failed, create→vm-create-failed,<br/>wait-ip→lease-timeout, ssh-verify→ssh-timeout)<br/>JSON: error envelope + partial run_state (vm/image sections already earned)<br/>text: vm-create: message — exit 1"]
    P -->|success| Q{"--json?"}
    Q -->|yes| R["emit success envelope: booted + vm{name,uuid,state,specs,mac,ip,<br/>dns_name,ssh_user,ssh/console/teardown commands,autostart} + image{...}"]
    Q -->|no| S["text: Name/UUID, Distro, IP (+ name.default), SSH hint,<br/>mount lines, Console/Teardown lines"]
```

Key invariants of the pipeline:

- **One `try`, one `stage` variable.** A failure at any stage is caught once;
  the stage name picks the default error code, and `run_state` (filled as
  stages complete) is rendered into the JSON error document, so a late failure
  (e.g. lease timeout) still reports the running VM and its image.
- **Nothing left behind.** Failed volume import, failed `virt-install`, or a
  missing keypair all clean up after themselves (PI-12).
- **IP discovery is lease-file-first.** `domifaddr`/`arp` are best-effort
  fallbacks only when the dnsmasq lease file is unusable (PI-7/PI-8).

### 3.2 `destroy`

```mermaid
flowchart TD
    A["virt-runner destroy NAME [--json]"] --> B["set_json_mode; v = Virtualizer()"]
    B --> C{"require_libvirt"}
    C -->|fail| X1["fail: libvirt-unreachable, exit 1"]
    C --> D{"domain_exists(name)?"}
    D -->|no| X2["fail: VM 'NAME' is not defined (code vm-not-defined), exit 1"]
    D --> E["destroy_vm(name):"]
    E --> E1{"domain running?"}
    E1 -->|yes| E2["virsh destroy"]
    E1 -->|no| E3["skip destroy"]
    E2 --> F["virsh undefine (mandatory: vol-delete fails on attached disk)"]
    E3 --> F
    F --> G["virsh vol-delete NAME_vda.qcow2 vm-pool"]
    G --> G1{"outcome?"}
    G1 -->|deleted| H
    G1 -->|"not found / no such volume"| W["D7: warning, not failure —<br/>volume already absent"]
    G1 -->|other error| X3["fail: volume-delete-failed, exit 1"]
    W --> H
    H --> I{"--json?"}
    I -->|yes| J["envelope: name, domain{was_running, destroyed, undefined},<br/>volume{name, pool, deleted, already_absent}, warnings[]"]
    I -->|no| K["text: confirmation line (+ warning line)"]
```

### 3.3 `list`

```mermaid
flowchart TD
    A["virt-runner list [--user U] [--json]"] --> B["set_json_mode; v = Virtualizer()"]
    B --> C["narrowed preflight: require_libvirt + require_pool_defined<br/>(pool may be INACTIVE — VMs are still listable; network irrelevant)"]
    C -->|fail| X["fail_with: default pool-not-found,<br/>but the carried code wins (libvirt-unreachable,<br/>pool-not-found, pool-path-unknown) — exit 1"]
    C --> D["list_vms(lease_file):"]
    D --> D1["pool path from pool-dumpxml<br/>virsh list --all --name"]
    D1 --> D2{"each domain: dumpxml,<br/>disk source under pool path?<br/>(path-component match, no sibling-prefix trap)"}
    D2 -->|no — not ours| D3["skip"]
    D2 -->|yes| D4["VmInfo: state (domstate), mac, ip from lease file,<br/>ip_status = ip_status_for(state, mac, ip):<br/>no-mac ≻ not-running ≻ lease ≻ running-no-lease,<br/>meta from domain metadata"]
    D4 --> E{"--json?"}
    E -->|yes| F["envelope: pool, user_override, count, vms[]<br/>ip/ssh_user null unless ip_status == lease<br/>user: --user flag ≻ recorded metadata ≻ 'ubuntu'"]
    E -->|no| G{"any VMs?"}
    G -->|no| G1["hint: no VMs in pool"]
    G -->|yes| G2["one text block per VM (same VmInfo,<br/>cannot drift from the JSON view):<br/>Name/UUID, IP, SSH hint, Console, Teardown"]
```

### 3.4 `ssh`

```mermaid
flowchart TD
    A["virt-runner ssh NAME [--user U] [--json]"] --> B["set_json_mode; v = Virtualizer()"]
    B --> C{"require_libvirt"}
    C -->|fail| X1["fail: libvirt-unreachable, exit 1"]
    C --> D{"domain_exists(name)?"}
    D -->|no| X2["fail: VM not defined (vm-not-defined), exit 1"]
    D --> E{"domstate == running?"}
    E -->|no| X3["fail: VM is <state> — start it first (vm-not-running), exit 1"]
    E --> F["vm_ip: non-blocking — MAC → lease file,<br/>then single domifaddr sweep (IP may carry /prefix)"]
    F -->|none| X4["fail: no IP found (no-ip), exit 1"]
    F -->|ip| G["resolve identity + user:<br/>--user flag ≻ domain metadata (recorded at create) ≻ 'ubuntu'<br/>identity ≻ metadata ≻ ~/.ssh/virt_runner_key"]
    G --> H["ssh_shell: ssh -o StrictHostKeyChecking=no<br/>-o UserKnownHostsFile=/dev/null -i <identity> user@ip<br/>via _passthrough — real interactive shell,<br/>stdout steered to stderr under --json"]
    H --> I["exit with ssh's own exit code"]
    I --> J{"--json?"}
    J -->|yes| K["envelope: name, ip, ssh_user, exit_code, ssh/console/teardown commands"]
    J -->|no| L["(shell output was the output)"]
```

### 3.5 Cross-cutting: `--json` mode and error plumbing

```mermaid
sequenceDiagram
    participant User
    participant JC as JsonCommand (args.py)
    participant CMD as cmd_* body
    participant OUT as output.py
    participant V as Virtualizer

    User->>JC: virt-runner <cmd> ... --json
    Note over JC: pre-scans raw args for --json BEFORE parsing
    alt usage error (bad flag/arg)
        JC->>OUT: set_json_mode(True) + fail(code=usage, exit_code=2)
        OUT-->>User: one JSON error document on stdout, exit 2
    end
    JC->>OUT: set_json_mode(as_json)
    JC->>CMD: invoke with parsed params
    CMD->>V: pipeline calls (progress lines → OUT.progress, silent in --json)
    alt success
        CMD->>OUT: emit(cmd, fields)
        OUT-->>User: one JSON success document, exit 0
    else RuntimeError
        CMD->>OUT: fail_with(cmd, exc, stage_default_code, fields=partial state)
        Note over OUT: code = exc.code (VirtError) or stage default
        OUT-->>User: one JSON error document (+ partial sections), exit 1
    end
```
