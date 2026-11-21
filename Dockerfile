# The scorer builds this on linux/amd64 and runs it with no network. The
# digest is the image we labeled every commit in: its Python and SQLite
# decide which sqlglot tests run, so keep it. We never run sqlglot's tests at
# run time, but the same interpreter keeps our maps and the labels in sync.
FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90

# Everything the image needs is committed in the repository: one COPY per
# input, no network at build beyond the base image pull, no sqlglot clone and
# no pip install. Order is least-volatile first -- selector.py, the
# blastradius package, then maps/ (regenerated often), so a map regeneration
# only rebuilds the maps layer and the chmod below, never the code layers.

COPY selector.py /app/selector.py
COPY blastradius/ /app/blastradius/
COPY maps/ /app/maps/

# The container runs as user nobody on a read-only disk, so every file under
# /app must be world-readable and every directory world-executable no matter
# what mode or umask it had on the host that built it. This is the only RUN.
RUN chmod -R a+rX /app

CMD ["python", "/app/selector.py"]
