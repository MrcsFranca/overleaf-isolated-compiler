# Overleaf CE — Sandbox Compile Proxy

Compilation sandbox for **Overleaf Community Edition** using Docker-out-of-Docker (DooD). Each LaTeX document is compiled inside an isolated, ephemeral `texlive` container, replicating the behavior of Overleaf Server Pro without modifying Overleaf's source code.

---

## How it works

By default, Overleaf CE compiles LaTeX directly inside the `sharelatex` container. This project intercepts those compilation requests and routes them through an isolated sandbox container per build.

```
[Overleaf (Node.js/ClsiManager)]
        │
        │  POST http://proxy:3013/project/{id}/user/{uid}/compile
        ▼
[Flask Proxy Container]  ──── docker.sock ────►  [Host Docker Daemon]
        │                                                  │
        │  /tmp/sandbox/{build_id}/                        │ spawns
        ▼                                                  ▼
[texlive/texlive container]  ◄─────────────────────────────┘
  - network_disabled=True
  - mem_limit=512m
  - removed after compile (remove=True)
        │
        │  output.pdf, output.log, ...
        ▼
[/var/lib/overleaf/data/output/{project_id}-{user_id}/generated-files/{build_id}/]
        │
        ▼
[nginx (clsi-nginx) serves PDF to browser]
```

**Compilation flow:**

1. User clicks Recompile → browser POSTs to Overleaf web service
2. `ClsiManager` (Node.js) forwards the request to the proxy at `http://proxy:3013`
3. Proxy writes `.tex` source files to an isolated build directory in `/tmp/sandbox/{build_id}/`
4. Proxy creates a `texlive/texlive` sibling container with that directory mounted
5. `latexmk` compiles the document inside the container; container is destroyed on exit
6. Proxy copies `output.*` files to the path the Overleaf-internal nginx already serves
7. Proxy responds with `{compile: {status, outputFiles}}` — the format expected by `ClsiManager`
8. Browser requests the PDF URL; nginx serves it from the shared filesystem

---

## Requirements

- Overleaf CE deployed via [overleaf-toolkit](https://github.com/overleaf/toolkit)
- Docker CE installed on the host (**not** via snap)
- Docker socket accessible at `/var/run/docker.sock`
- `texlive/texlive` image pulled on the host:

```bash
docker pull texlive/texlive
```

---

## Repository structure

```
proxy/
├── app.py                 # Flask proxy — compilation orchestrator
├── Dockerfile             # Proxy container image
└── settings.custom.js    # Overleaf settings override (one line changed)

overleaf-toolkit/
└── lib/
    └── docker-compose.base.yml   # Modified to add the proxy service
```

---

## Installation

### 1. Clone this repository alongside overleaf-toolkit

```
overleaf-toolkit/          # your existing Overleaf CE installation
proxy/                     # this repository (clone it here)
```

### 2. Modify `overleaf-toolkit/lib/docker-compose.base.yml`

Add the volume mount to `sharelatex` and the `proxy` service:

```yaml
services:
  sharelatex:
    volumes:
      - "${OVERLEAF_DATA_PATH}:${OVERLEAF_IN_CONTAINER_DATA_PATH}"
      # Routes compilation requests to the proxy (only line changed in settings)
      - ../../proxy/settings.custom.js:/etc/overleaf/settings.js
    # ... rest of sharelatex config unchanged ...

  proxy:
    build: ../../proxy
    container_name: proxy
    restart: always
    volumes:
      # Allows proxy to create sibling Docker containers
      - /var/run/docker.sock:/var/run/docker.sock
      # Shared compilation workspace (host path, resolved by Docker daemon)
      - /tmp/sandbox:/tmp/sandbox
      # Same data volume as sharelatex — proxy writes here, nginx reads from here
      - "${OVERLEAF_DATA_PATH}:${OVERLEAF_IN_CONTAINER_DATA_PATH}"
```

### 3. Update `settings.custom.js`

The only change from the default Overleaf CE `settings.js` is one line inside `apis.clsi`:

```javascript
apis: {
  clsi: {
    url: 'http://proxy:3013',  // was: 'http://127.0.0.1:3013'
  },
  // ... rest unchanged
}
```

To get a base file matching your exact Overleaf CE version:

```bash
docker cp sharelatex:/etc/overleaf/settings.js ./proxy/settings.custom.js
# Then change only the apis.clsi.url line above
```

### 4. Build and start

```bash
cd overleaf-toolkit
./bin/docker-compose up -d --build proxy
./bin/docker-compose restart sharelatex   # required to apply the new settings volume
```

### 5. Verify

```bash
# Proxy should respond with {"status": "proxy online"}
docker exec sharelatex curl -s http://proxy:3013/status

# Watch containers during a compile — a texlive/texlive container should appear briefly
watch -n 0.5 docker ps --format "table {{.Names}}\t{{.Image}}\t{{.Status}}"
```

---

## Security

| Feature | Detail |
|---|---|
| Network isolation | Each `texlive` container runs with `network_disabled=True` — no outbound access |
| Memory cap | `mem_limit=512m` prevents runaway compilations from affecting the host |
| Stateless sandbox | Containers are destroyed immediately after compilation (`remove=True`) |
| Filesystem scope | Compilation sandbox is cleaned up after each build; only `output.*` files are kept |

---

## License

This project has the [MIT](https://choosealicense.com/licenses/mit/) license.
