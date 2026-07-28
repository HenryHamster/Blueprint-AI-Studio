# Blueprint self-hosted LiveKit gateway

This is the small, production-oriented v1 deployment for Resident call mode:
one always-on GCE VM, one supervised Compose stack, and no LiveKit task
processes. The Resident Coordinator joins outbound from its assigned runner;
the browser joins the public LiveKit endpoint with a Blueprint-issued,
short-lived room token.

It intentionally is **not** Kubernetes, a general voice platform, an audio
archive, or a second task queue. LiveKit has no recording, egress, ingress, or
webhook configuration here. Blueprint remains the canonical transcript and
authorization system.

## Non-negotiable host boundary

Use a dedicated `voice-gateway` GCE VM managed alongside the runner fleet, not
an active Blueprint task daemon or any host advertising agent/compute capacity.
The current Blueprint task isolation keeps a shared network namespace; processes
on a co-hosted agent runner could otherwise call the GCE metadata service and
use the VM service account to read LiveKit secrets. A runner may be installed
only after its task network sandbox blocks `169.254.169.254` and the IPv6
metadata endpoint for every task process. This is a security boundary, not a
scaling preference.

The VM reuses the `blueprint-runner` checkout and operator CLI conventions, but
it is not registered as task capacity. LiveKit health remains independent of
runner heartbeat because only an external browser/TURN probe can prove the
public media path. The systemd service is independent of runner claims, task
attempts, and Resident process generations.

## One-time GCP setup

Choose a stable DNS name, for example `livekit.example.com`, and point its A
record at the VM's reserved external IPv4 address before starting Caddy. Caddy
obtains the trusted HTTPS/WSS certificate through HTTP-01; keep TCP 80 and 443
open to the Internet for that purpose. Do not use a self-signed certificate.

Also reserve a second address and point `turn.example.com` at a GCE external
**TCP proxy** load balancer. The proxy must pass TLS through on external TCP
443 to the voice-gateway VM's TCP 5349 backend. The LiveKit container listens
on internal TCP 443, Compose maps that to host 5349, and LiveKit advertises
443 to clients. This separate L4 address is required because a reverse proxy
that owns the HTTPS/WSS port cannot also terminate LiveKit's embedded TURN/TLS
traffic. It is the small single-VM way to satisfy restrictive-network TURN/TLS
without an unsafe port collision.

Create a dedicated service account and grant it access to exactly three secret
resources, not the project-wide `secretAccessor` role. GCE's `cloud-platform`
scope is only the transport scope; resource-level IAM below is the permission
boundary.

```bash
PROJECT_ID=your-gcp-project
VM_SA=blueprint-livekit@${PROJECT_ID}.iam.gserviceaccount.com
gcloud iam service-accounts create blueprint-livekit --project="$PROJECT_ID"

# Create these from trusted local files/stdin. Each production config/version is
# immutable: use an explicit numeric version in secret-refs, never `latest`.
gcloud secrets create blueprint-livekit-config --replication-policy=automatic --project="$PROJECT_ID"
gcloud secrets create blueprint-livekit-turn-cert --replication-policy=automatic --project="$PROJECT_ID"
gcloud secrets create blueprint-livekit-turn-key --replication-policy=automatic --project="$PROJECT_ID"

for SECRET in blueprint-livekit-config blueprint-livekit-turn-cert blueprint-livekit-turn-key; do
  gcloud secrets add-iam-policy-binding "$SECRET" --project="$PROJECT_ID" \
    --member="serviceAccount:${VM_SA}" --role="roles/secretmanager.secretAccessor"
done
```

Attach that service account to the dedicated VM. Do not place a service-account
JSON key on disk. The host obtains short-lived metadata credentials only while
`render-livekit-secrets` runs. The server containers get root-owned runtime
files and never receive Google credentials, `gcloud`, or Secret Manager names.

The config secret is a production copy of
[`livekit.yaml.example`](livekit.yaml.example), with an opaque LiveKit API key
and secret inserted under `keys:`. The cert and key secrets contain PEM only.
For example, after adding the initial versions:

```bash
sudo install -d -m 0700 -o root -g root /etc/blueprint-livekit
sudo tee /etc/blueprint-livekit/secret-refs >/dev/null <<'EOF'
LIVEKIT_CONFIG_SECRET=projects/your-gcp-project/secrets/blueprint-livekit-config/versions/1
LIVEKIT_TURN_CERT_SECRET=projects/your-gcp-project/secrets/blueprint-livekit-turn-cert/versions/1
LIVEKIT_TURN_KEY_SECRET=projects/your-gcp-project/secrets/blueprint-livekit-turn-key/versions/1
EOF
sudo chmod 0600 /etc/blueprint-livekit/secret-refs

sudo tee /etc/blueprint-livekit/public.env >/dev/null <<'EOF'
LIVEKIT_PUBLIC_HOST=livekit.example.com
LIVEKIT_TURN_HOST=turn.example.com
CADDY_ACME_EMAIL=ops@example.com
EOF
sudo chmod 0600 /etc/blueprint-livekit/public.env
```

The `secret-refs` file is deliberately non-secret but root-readable only: its
explicit version pins make a configuration change reviewable and rollbackable.
The production config's `turn.domain` must exactly equal `LIVEKIT_TURN_HOST`;
it must not be `LIVEKIT_PUBLIC_HOST`. The gateway preflight fetches the pinned
config into a temporary protected directory and rejects that mismatch before a
service start or reload. The TURN certificate must cover that same TURN host.

## Backend token and speech credentials

The gateway identity above is intentionally unable to read application
credentials. Use the backend service's existing runtime identity, not the VM
service account, and give it resource-level access only to the four call
secrets below. In particular, it must not be able to read
`blueprint-livekit-config`, the TURN certificate, or the TURN key.

```bash
PROJECT_ID=your-gcp-project
BACKEND_SA=blueprint-backend@${PROJECT_ID}.iam.gserviceaccount.com

# The values in these first two secrets must be the same API key and API secret
# in the pinned LiveKit config's `keys:` map. Add immutable versions from a
# trusted workstation; do not place their values in a shell history or repo.
gcloud secrets create blueprint-livekit-api-key --replication-policy=automatic --project="$PROJECT_ID"
gcloud secrets create blueprint-livekit-api-secret --replication-policy=automatic --project="$PROJECT_ID"
gcloud secrets create blueprint-call-deepgram-api-key --replication-policy=automatic --project="$PROJECT_ID"
gcloud secrets create blueprint-call-cartesia-api-key --replication-policy=automatic --project="$PROJECT_ID"

for SECRET in blueprint-livekit-api-key blueprint-livekit-api-secret \
  blueprint-call-deepgram-api-key blueprint-call-cartesia-api-key; do
  gcloud secrets add-iam-policy-binding "$SECRET" --project="$PROJECT_ID" \
    --member="serviceAccount:${BACKEND_SA}" --role="roles/secretmanager.secretAccessor"
done
```

Configure the backend deployment with this exact environment contract. The
deployment platform injects the two LiveKit values as secret values; the speech
settings deliberately remain numbered Secret Manager *resource names*, because
the backend resolves them only for an authenticated persistent Resident task.
`LIVEKIT_URL` is the browser/Coordinator WSS address, never the TURN address.

```dotenv
LIVEKIT_URL=wss://livekit.example.com
# Platform secret mounts, pinned to explicit versions:
LIVEKIT_API_KEY=blueprint-livekit-api-key:1
LIVEKIT_API_SECRET=blueprint-livekit-api-secret:1

# Backend runtime configuration, not secret payloads:
CALL_DEEPGRAM_API_KEY_SECRET_VERSION=projects/your-gcp-project/secrets/blueprint-call-deepgram-api-key/versions/1
CALL_CARTESIA_API_KEY_SECRET_VERSION=projects/your-gcp-project/secrets/blueprint-call-cartesia-api-key/versions/1
CALL_DEEPGRAM_STT_MODEL=flux-general-en
CALL_CARTESIA_TTS_MODEL=sonic-3
CALL_CARTESIA_VOICE_ID=your-reviewed-cartesia-voice-id
```

For example, a Cloud Run deployment maps the first two as version-pinned secret
environment variables and the rest as ordinary environment variables. Substitute
the actual backend service name/region, preserve unrelated service settings, and
use the identity above for the service revision.

```bash
gcloud run services update blueprint-backend --region=your-region --project="$PROJECT_ID" \
  --update-secrets=LIVEKIT_API_KEY=blueprint-livekit-api-key:1,LIVEKIT_API_SECRET=blueprint-livekit-api-secret:1 \
  --update-env-vars=LIVEKIT_URL=wss://livekit.example.com,CALL_DEEPGRAM_API_KEY_SECRET_VERSION=projects/your-gcp-project/secrets/blueprint-call-deepgram-api-key/versions/1,CALL_CARTESIA_API_KEY_SECRET_VERSION=projects/your-gcp-project/secrets/blueprint-call-cartesia-api-key/versions/1,CALL_DEEPGRAM_STT_MODEL=flux-general-en,CALL_CARTESIA_TTS_MODEL=sonic-3,CALL_CARTESIA_VOICE_ID=your-reviewed-cartesia-voice-id
```

Never send any of these values to the browser or a runner host. The backend
issues only short-lived participant tokens and vends speech credentials only to
the exact active-call Coordinator endpoint; they never enter the Resident
process environment.

The Fast adapter remains fail-closed without
`CALL_FOREGROUND_QUALIFICATION=claude-warm-stream-json:phase0-v1`. Set that
attestation only on an isolated staging revision while exercising the
real-account exit gates; promote it to the production backend only after those
gates pass. The Coordinator receives the non-secret attestation through the
same fenced endpoint; do not configure it on a runner host.

### Credential rotation

The gateway renderer atomically replaces its three mounted files, but a
single-node reload interrupts media. Schedule API-key rotation outside active
calls. Rotate the LiveKit key pair with overlap so a backend revision can never
mint a token the gateway rejects:

1. Create numbered versions for a new API key and secret. Create a new
   `blueprint-livekit-config` version whose `keys:` map contains both the old
   and new pairs; update only `LIVEKIT_CONFIG_SECRET` to that explicit version.
2. Run `livekit-preflight --system`, then `livekit-reload --system`; the
   gateway now accepts both keys. Deploy one backend revision pinned to the new
   `LIVEKIT_API_KEY` and `LIVEKIT_API_SECRET` versions, and verify a real call.
3. After the maximum issued token lifetime has elapsed and old calls are gone,
   create a final config version that removes the old pair, pin it in
   `secret-refs`, preflight, and reload. Retain the old Secret Manager versions
   through the rollback window rather than deleting them.

Rotate a speech-provider secret by creating a new numbered version and
deploying one backend revision with the corresponding
`CALL_*_API_KEY_SECRET_VERSION` value. Drain active calls first when a provider
does not accept both credentials; keep the prior version until the new call and
rollback checks pass. This keeps every call process on one stable provider key
for its lifetime.

## Install and start

Install Docker Engine plus the Docker Compose v2 plugin and `gcloud` on the
dedicated VM. Keep the Blueprint checkout durable on that VM, then use the
same `blueprint-runner` launcher conventions that supervise the Strategic
runner. This creates an independent **system** service rooted in the checkout;
it is not task capacity and is never coupled to runner heartbeat or dispatch.

`livekit-preflight` rejects WSL2: WSL NAT is not a reliable public WebRTC/TURN
host. Choose an always-on Linux VM with no active Blueprint task daemon.
Installation writes the root-owned `/etc/blueprint-livekit-host-role` marker.
Both the launcher and a direct `local_runner.py` invocation fail closed while
that marker exists or the LiveKit unit is active, preventing later accidental
reuse of the gateway as task capacity.

```bash
cd /path/to/blueprint
sudo scripts/blueprint_runner.py livekit-preflight --system
sudo scripts/blueprint_runner.py livekit-install --system
sudo scripts/blueprint_runner.py livekit-status --system
sudo journalctl -u blueprint-livekit.service -n 100 --no-pager
```

The first successful Caddy start proves DNS, inbound TCP 80/443, and a trusted
certificate. The `ExecStartPost` check proves both the private LiveKit listener
and the local TLS proxy path. It does not prove public browser ICE or TURN.
Treat the documented real-browser/TURN smoke test and an external synthetic
check of `https://livekit.example.com/` as release gates; alert on failures.

## GCE firewall

Apply network tags only to the voice-gateway VM. Do **not** open these ports on
the normal runner pool.

| Direction | Ports | Purpose |
| --- | --- | --- |
| ingress TCP | 80, 443 | ACME challenge and HTTPS/WSS signaling on the VM address |
| ingress UDP | 3478 | embedded TURN/UDP |
| ingress TCP | 7881 | ICE/TCP fallback |
| ingress UDP | 50000-60000 | WebRTC media candidates |
| ingress TCP | 5349 from the GCE TCP proxy only | backend for `turn.example.com:443` |
| ingress | none for 7880 or 6789 | loopback-only signaling backend and Prometheus |
| egress | TCP 443 plus DNS/NTP as required | Google Secret Manager, ACME, and normal service operation |

The browser and Coordinator use `wss://livekit.example.com`; do not expose
`7880`. The browser receives `turn.example.com:443` from LiveKit. Validate a
restrictive-network client through TURN/TLS as part of the staging call
benchmark. The VM firewall must allow TCP 5349 only from the TCP proxy and its
health-check ranges; it is never a direct public listener.

## Release, upgrade, and rollback

Use a separate staging VM, DNS name, and three staging secrets. First verify a
real browser + Coordinator call: microphone, typed fallback, reconnect, and a
restrictive network/TURN case. Then pin the reviewed production image digest
and explicit Secret Manager version numbers in a commit. Never use `latest`.

For an upgrade, drain/end active calls, update only the image digest or secret
version references, then use the explicit companion reload. `blueprint-runner
update` updates the durable checkout but deliberately does **not** restart a
live media gateway on its own.

```bash
cd /path/to/blueprint
scripts/blueprint_runner.py update
sudo scripts/blueprint_runner.py livekit-reload --system
sudo scripts/blueprint_runner.py livekit-status --system
```

Rollback is a reverse edit to the previous image digest and the prior numbered
secret versions, followed by the same restart and real-call smoke test. Do not
delete old secret versions or Caddy's `/var/lib/blueprint-livekit/caddy-*` data
until the rollback window is over. Restarting the single-node server intentionally
interrupts active media; schedule it outside a call or end the call first.

## Operational limits

This v1 is single-node and private. It has no HA claim. Redis is intentionally
absent: LiveKit only needs it for distributed/multi-node mode. Add Redis, a
load balancer, and an availability design only after measured call volume or
uptime requirements justify them.
