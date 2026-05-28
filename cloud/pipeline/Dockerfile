FROM public.ecr.aws/lambda/python:3.11

WORKDIR ${LAMBDA_TASK_ROOT}

# Build context is aws/ — paths below are relative to aws/
COPY pipeline/requirements_lambda.txt .
RUN pip install --no-cache-dir -r requirements_lambda.txt

# Pre-install DuckDB extensions into the image (same pattern as API Lambda).
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

COPY pipeline/*.py .
COPY sql/ sql/

CMD ["handler.lambda_handler"]
