from funcy import (zipdict, isums, walk_values, group_by, count_by, second, compose,
                   first, join_with, partial, compact)
from tqdm import tqdm
from handy.db import queryset_iterator
from datetime import datetime, timedelta
from django.db.models import Count
from django.db import transaction
from django.core.management import BaseCommand
from core.models import HistoricalCounter
from legacy.models import Series, Sample, Platform
from core.models import User
from tags.models import Tag, SerieAnnotation

MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']

def ceil_attrs_date(model):
    month, day, year = model.attrs.get('submission_date', 'Jan 1 1960').split()
    if month == 'Jans':
        month = 'Jan'
    return ceil_date(datetime(int(year), MONTHS.index(month) + 1, 1))

def ceil_date(date):
    next_month = datetime(date.year, date.month, 1) + timedelta(days=31)
    return datetime(next_month.year, next_month.month, 1)


START_DATE = datetime(2014, 11, 1)
CURRENT_DATE = ceil_date(datetime.now())

def accumulate(data):
    dates, counts = zip(*sorted(data.items()))
    return zipdict(dates, isums(counts))

def get_value(keys, index):
    """
    There is no secuence with two or more holes in data array,
    so we can do only one step back
    """
    def _getter(item):
        return item.get(
            keys[index],
            item.get(keys[index - 1 if index > 0 else index], 0))
    return _getter

class Command(BaseCommand):
    def handle(self, *args, **options):
        series = accumulate(count_by(ceil_attrs_date, Series.objects.all()))

        iterator = tqdm(queryset_iterator(Sample.objects.all(), 30000),
                        total=Sample.objects.count(),
                        desc='samples')
        samples = accumulate(count_by(ceil_attrs_date, iterator))

        platform_created_on = join_with(
            min,
            [{p: ceil_attrs_date(s) for p in s.platforms}
             for s in Series.objects.all()])
        qs = Platform.objects.annotate(probes_count=Count('probes'))\
                             .values('gpl_name', 'probes_count')
        platforms_data = [
            [platform_created_on[item['gpl_name']], item['probes_count']]
            for item in qs
        ]
        platforms = accumulate(count_by(first, platforms_data))
        group = group_by(first, platforms_data)
        platforms_probes = accumulate(walk_values(
            compose(sum, compact, partial(map, second)), group))

        users = accumulate(count_by(ceil_date, User.objects.values_list('date_joined', flat=True)))

        tags = accumulate(count_by(ceil_date, Tag.objects.values_list('created_on', flat=True)))

        qs = SerieAnnotation.objects.values_list('created_on', flat=True)
        serie_annotations = accumulate(count_by(ceil_date, qs))

        values = SerieAnnotation.objects.all()\
            .annotate(samples_annotation_count=Count('sample_annotations'))\
            .values_list('created_on', 'samples_annotation_count')

        group = group_by(compose(ceil_date, first), values)
        sample_annotations = accumulate(walk_values(
            compose(sum, partial(map, second)), group))

        keys = sorted(
            [key for key in set(series.keys() +
                                samples.keys() +
                                platforms.keys() +
                                platforms_probes.keys() +
                                users.keys() +
                                tags.keys() +
                                sample_annotations.keys())
             if key >= START_DATE and key < CURRENT_DATE])

        data = {
            'series': series,
            'samples': samples,
            'platforms': platforms,
            'platforms_probes': platforms_probes,
            'users': users,
            'tags': tags,
            'serie_annotations': serie_annotations,
            'sample_annotations': sample_annotations,
        }

        with transaction.atomic():
            HistoricalCounter.objects.filter(created_on__lte=CURRENT_DATE).delete()
            HistoricalCounter.objects.bulk_create([
                HistoricalCounter(
                    created_on=key,
                    counters=walk_values(get_value(keys, index), data))
                for index, key in enumerate(keys)])
