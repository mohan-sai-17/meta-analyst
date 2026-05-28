FROM public.ecr.aws/lambda/python:3.11

WORKDIR ${LAMBDA_TASK_ROOT}

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Pre-install DuckDB's httpfs + aws extensions into a fixed path inside the image.
# Lambda's filesystem is read-only except /tmp, but DuckDB can LOAD
# extensions from read-only paths — it only needs write access to INSTALL them.
# httpfs = S3 file system access, aws = CREDENTIAL_CHAIN auth provider.
ENV DUCKDB_EXTENSION_DIRECTORY=/var/task/.duckdb_ext
ENV HOME=/tmp
RUN python3 -c "\
import os, duckdb; \
os.makedirs('/var/task/.duckdb_ext', exist_ok=True); \
c = duckdb.connect(); \
c.execute(\"SET home_directory='/tmp';\"); \
c.execute(\"INSTALL httpfs;\"); \
c.execute(\"LOAD httpfs;\"); \
c.execute(\"INSTALL aws;\"); \
c.execute(\"LOAD aws;\"); \
print('httpfs + aws extensions installed OK')"

COPY main.py .

# Lambda handler: module.handler_function
CMD ["main.handler"]
