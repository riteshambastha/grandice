# The sandbox execution image (§06) — what the DockerSandbox backend runs
# each exec in. Not the harness itself; see ../DEPLOY_AWS.md.
#
# Bakes in the libraries the document skills need. The sandbox has no
# network access by design (--network none), so a skill can never
# `pip install` at runtime — anything it needs has to already be in this
# image. Add a library here, not in pyproject.toml, if only the sandboxed
# exec (not the harness process itself) needs it.
#
# Build once, referenced by GRANDICE_SANDBOX_IMAGE:
#   docker build -t grandice-sandbox:py3.12 sandbox/
FROM python:3.12-slim

RUN pip install --no-cache-dir \
        openpyxl==3.1.* \
        python-pptx==1.0.* \
        python-docx==1.2.* \
        pypdf==6.*
