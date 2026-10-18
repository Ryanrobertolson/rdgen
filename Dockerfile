FROM python:3.13-alpine

RUN adduser -D user
USER user

WORKDIR /opt/rdgen

COPY --chown=user:user . .
RUN pip install --no-cache-dir -r requirements.txt \
 && python manage.py migrate

ENV PYTHONUNBUFFERED=1

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --retries=3 CMD python -c 'import os,urllib.request; C=type("NoRedirect",(urllib.request.HTTPRedirectHandler,),{"redirect_request":lambda *args:None}); o=urllib.request.build_opener(C); r=urllib.request.Request("http://127.0.0.1:8000/",headers={"Authorization":"Bearer "+os.environ["RDGEN_API_TOKEN"]}); o.open(r,timeout=4).close()'

CMD ["/home/user/.local/bin/gunicorn", "-c", "gunicorn.conf.py", "rdgen.wsgi:application"]
