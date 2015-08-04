import json
from itertools import groupby

import jinja2
from django_jinja import library
from django.db.models.query import QuerySet


@library.filter(name='json')
def _json(value):
    if isinstance(value, QuerySet):
        value = list(value)
    return json.dumps(value)


@library.global_function
@jinja2.contextfunction
def replace_get(context, **kwargs):
    request = context.get('request')
    query = request.GET.copy()
    query.update(kwargs)
    return '%s?%s' % (request.path, query.urlencode())


@library.filter
def index(value, attribute):
    res = groupby(value, key=lambda o: getattr(o, attribute)[0].upper())
    return [(key, list(group)) for key, group in res]
