"""
Overleaf Docker-out-of-Docker (DooD) Compilation Proxy
This service intercepts compilation requests from Overleaf Community Edition,
runs them in an isolated, ephemeral Docker container (texlive), and returns
the generated PDF back to the Overleaf frontend, simulating the behavior of Overleaf Server Pro.

Fluxo:
  1. Overleaf (Node.js/ClsiManager) envia POST /project/{id}/user/{uid}/compile
  2. Este proxy escreve os arquivos .tex em /tmp/sandbox/{build_id}/
  3. Um container texlive/texlive efêmero compila em isolamento total
  4. Os arquivos output.* são copiados para o caminho que o nginx interno lê
  5. Resposta no formato {compile: {status, outputFiles}} é devolvida ao Overleaf

Stream:
    1. Overleaf (Node.js/ClsiManager) sends POST /project/{id}/user/{uid}/compile
    2. This proxy writes the .tex files in /tmp/sandbox/{build_id}/
    3. An ephemeral container texlive/texlive compiles the .tex file in a full isolated environment
    4. The output.* files are copied to the path nginx reads
    5. {compile: {status, outputFiles}} format is returned in Response
"""

from flask import Flask, request, jsonify
import docker
import os
import shutil
from datetime import datetime, timezone

app = Flask(__name__)
client = docker.from_env()

# Paths must match the volume bindings in docker-compose
BASE_SANDBOX = '/tmp/sandbox'
BASE_OUTPUT  = '/var/lib/overleaf/data/output'

os.makedirs(BASE_SANDBOX, exist_ok=True)
os.makedirs(BASE_OUTPUT,  exist_ok=True)

@app.route('/project/<project_id>/user/<user_id>/compile', methods=['POST'])
def intercept_compilation(project_id, user_id):
    """
    Intercepts the POST request from Overleaf, extracts the LaTeX source code,
    orchestrates a temporary compilation container, and moves the output
    files to the expected Overleaf data directory.
    """

    print(f"\n[INFO] Starting compilation", flush=True)

    compile_data = request.json.get('compile', {})
    options   = compile_data.get('options', {})
    build_id  = options.get('buildId', 'build-padrao')
    compiler  = options.get('compiler', 'pdflatex')
    root_tex  = compile_data.get('rootResourcePath', 'main.tex')

    print(f"  User     : {user_id}", flush=True)
    print(f"  Project  : {project_id}", flush=True)
    print(f"  Build ID : {build_id}", flush=True)

    # Setup sandbox directory for this specific build
    build_dir = os.path.join(BASE_SANDBOX, build_id)
    os.makedirs(build_dir, exist_ok=True)

    # Extract and write LaTeX source code to the sandbox
    resources = compile_data.get('resources', [])
    for resource in resources:
        rel_path = resource.get('path', 'main.tex')
        content  = resource.get('content')
        if content is not None:
            dest = os.path.join(build_dir, rel_path)
            os.makedirs(os.path.dirname(os.path.abspath(dest)), exist_ok=True)
            with open(dest, 'w', encoding='utf-8') as f:
                f.write(content)

    # Determine compiler flags and execute latexmk inside the sandbox container
    compiler_flags = {'pdflatex': '-pdf', 'xelatex': '-xelatex', 'lualatex': '-lualatex'}
    flag = compiler_flags.get(compiler, '-pdf')
    cmd  = f'latexmk {flag} -interaction=nonstopmode -jobname=output -outdir=/app {root_tex}'

    # Orchestrate the ephemeral Docker container (Sandbox)
    exit_code = 0
    try:
        client.containers.run(
            'texlive/texlive', cmd,
            remove=True,
            volumes={build_dir: {'bind': '/app', 'mode': 'rw'}},
            working_dir='/app',
            network_disabled=True,
            mem_limit='512m',
        )
        print(f"[OK] Successful compilation", flush=True)
    except docker.errors.ContainerError as e:
        print(f"[WARNING] LaTeX returned error non-zero: {e}", flush=True)
        exit_code = 1
    except Exception as e:
        print(f"[FATAL ERROR] Docker failed: {e}", flush=True)
        return jsonify({"status": "error", "error": str(e)}), 500

    # Move generated files to the Overleaf output directory
    nginx_output_dir = os.path.join(
        BASE_OUTPUT, f"{project_id}-{user_id}", "generated-files", build_id
    )
    os.makedirs(nginx_output_dir, exist_ok=True)

    waited_files = [
        ('output.pdf',         'pdf'),
        ('output.log',         'log'),
        ('output.aux',         'aux'),
        ('output.fls',         'fls'),
        ('output.fdb_latexmk', 'fdb_latexmk'),
        ('output.synctex.gz',  'gz'),
    ]

    output_files = []
    pdf_size     = 0
    created_at   = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.000Z')

    for filename, ftype in waited_files:
        src = os.path.join(build_dir, filename)
        if not os.path.exists(src):
            continue

        dst = os.path.join(nginx_output_dir, filename)
        shutil.copy2(src, dst)

        # Ensure file is written to disk
        with open(dst, 'rb') as fh:
            os.fsync(fh.fileno())

        os.chmod(dst, 0o644)

        size = os.path.getsize(dst)
        print(f"[OK] {filename} → {dst} ({size} bytes)", flush=True)

        url = f"http://proxy:3013/project/{project_id}/user/{user_id}/build/{build_id}/output/{filename}"
        entry = {"path": f"output.{ftype}", "url": url, "type": ftype, "build": build_id}
        if ftype == 'pdf':
            entry['size']      = size
            entry['ranges']    = []
            entry['createdAt'] = created_at
            pdf_size = size

        output_files.append(entry)

    os.chmod(nginx_output_dir, 0o755)

    if pdf_size == 0:
        print("[ERROR] PDF not found after compilation!", flush=True)

    # Cleanup sandbox and evaluate final status
    shutil.rmtree(build_dir, ignore_errors=True)
    status = "success" if pdf_size > 0 else "failure"

    print(f"[INFO] Returning response: status={status}, pdf_size={pdf_size}", flush=True)

    # Return Overleaf-compliant JSON payload
    return jsonify({
        "compile": {
            "status"      : status,
            "outputFiles" : output_files,
            "outputFilesArchive": {
                "path": "output.zip",
                "url" : f"/project/{project_id}/user/{user_id}/build/{build_id}/output/output.zip",
                "type": "zip",
            },
            "compileGroup": "standard",
            "buildId"     : build_id,
            "stats": {
                "latexmk-errors": exit_code, "latex-runs": 1,
                "latex-runs-with-errors": exit_code,
                "latex-runs-1": 1, "latex-runs-with-errors-1": exit_code,
                "pdf-size": pdf_size,
            },
            "timings": {"sync": 0, "compile": 100, "output": 0, "compileE2E": 100},
            "outputUrlPrefix": "",
        }
    })

@app.route('/status', methods=['GET'])
def health():
    return jsonify({"status": "proxy online"})

if __name__ == "__main__":
    app.run(host='0.0.0.0', port=3013, threaded=False)
