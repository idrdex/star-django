import os
import re
import gzip
import urllib2
import shutil

from easydict import EasyDict
from funcy import first, log_durations, imap, memoize, without, make_lookuper
from handy.db import db_execute
import numpy as np
import pandas as pd
from django.db import connections, transaction

from legacy.models import Sample, Series, Platform, PlatformProbe, Analysis, MetaAnalysis

import logging
logger = logging.getLogger("stargeo.analysis")
logger.setLevel(logging.DEBUG)


SERIES_MATRIX_URL = 'ftp://ftp.ncbi.nih.gov/pub/geo/DATA/SeriesMatrix/'
SERIES_MATRIX_MIRROR = os.environ['SERIES_MATRIX_MIRROR']


@log_durations(logger.debug)
def task_analyze(analysis_name, description, case_query, control_query, modifier_query,
                 debug=False):
    logger.info('Started %s analysis', analysis_name)
    with log_durations(logger.debug, 'Loading dataframe for %s' % analysis_name):
        df = get_analysis_df(case_query, control_query, modifier_query)
    debug and df.to_csv("%s.analysis_df.csv" % analysis_name)

    # Load GSE data, make and concat all fold change analyses results.
    # NOTE: we are doing load_gse() lazily here to avoid loading all matrices at once.
    logger.info('Loading data and calculating fold changes for %s', analysis_name)
    with log_durations(logger.debug, 'Fold changes for %s' % analysis_name):
        gses = (load_gse(df, series_id) for series_id in sorted(df.series_id.unique()))
        fold_changes = pd.concat(imap(get_fold_change_analysis, gses))
        debug and fold_changes.to_csv("%s.fc.csv" % debug)

    logger.info('Meta-Analyzing %s', analysis_name)
    with log_durations(logger.debug, 'Meta analysis for %s' % analysis_name):
        balanced = getFullMetaAnalysis(fold_changes, debug=debug).reset_index()
        debug and balanced.to_csv("%s.meta.csv" % debug)

    logger.info('Inserting %s analysis results', analysis_name)
    with log_durations(logger.debug, 'Saving results of %s' % analysis_name), \
            transaction.atomic('legacy'):
        analysis = Analysis.objects.create(
            analysis_name=analysis_name,
            description=description,
            case_query=case_query,
            control_query=control_query,
            modifier_query=modifier_query,
            series_count=len(df.series_id.unique()),
            platform_count=len(df.platform_id.unique()),
            sample_count=len(df.sample_id.unique()),
            series_ids=df.series_id.unique().tolist(),
            platform_ids=df.platform_id.unique().tolist(),
            sample_ids=df.sample_id.unique().tolist(),
        )
        balanced['analysis'] = analysis
        # replace infs with NaN for db insert
        # balanced = balanced.replace([np.inf, -np.inf], np.nan)
        # http://stackoverflow.com/questions/14162723/replacing-pandas-or-numpy-nan-with-a-none-to-use-with-mysqldb
        # balanced = balanced.where((pd.notnull(balanced)), None)
        balanced.columns = balanced.columns.map(lambda x: x.replace(".", "_").lower())
        field_names = [f.name for f in MetaAnalysis._meta.fields if f.name != 'id']
        rows = balanced[field_names].T.to_dict().values()
        MetaAnalysis.objects.bulk_create(MetaAnalysis(**row) for row in rows)

    logger.info('DONE %s analysis', analysis_name)


# from debug_cache import DebugCache
# dcache_new = DebugCache('/home/suor/projects/health/debug_cache_new')
# dcache_tmp = DebugCache('/home/suor/projects/health/debug_cache_tmp')


# @dcache.checked
# @dcache_new.cached
@log_durations(logger.debug)
def get_fold_change_analysis(gse):
    # TODO: get rid of unneeded OOP interface
    logger.debug('Calculating fold change for %s', gse.name)
    return GseAnalyzer(gse).getResults(debug=False)


@memoize
def _get_columns(table):
    with db_execute('select * from %s limit 1' % table, (), 'legacy') as cursor:
        return [col.name for col in cursor.description]


def get_full_df():
    # tags_qs = Tag.objects.values_list('tag_name', flat=True).order_by('tag_name').distinct()
    # tags = [tag.lower() for tag in tags_qs]
    non_tags = ['id', 'doc', 'series_id', 'platform_id', 'sample_id']
    tags = without(_get_columns('sample_tag_view'), *non_tags)

    view_fields = ['id', 'series_id', 'platform_id', 'sample_id'] + tags
    fields = ['sample_tag_view.{0} as "sample_tag_view.{0}"'.format(f) for f in view_fields]
    for cls in (Sample, Series, Platform):
        fields += ['{0}.{1} as "{0}.{1}"'.format(cls._meta.db_table, field.column)
                   for field in cls._meta.fields]
    fields_sql = ','.join(fields)

    df = pd.read_sql_query('''
        select %s
            from sample_tag_view
            join sample on (sample_tag_view.sample_id = sample.id)
            join series on (sample_tag_view.series_id = series.id)
            join platform on (sample_tag_view.platform_id = platform.id)
    ''' % fields_sql, connections['legacy']).convert_objects(convert_numeric=True)

    clean_columns = []
    clean_series = []

    field_names = ['gse_name', 'gpl_name', 'gsm_name', "series_id", "sample_id", "platform_id"]
    for col in df.columns:
        table, header = col.split(".")
        field = header.lower()
        if (field in tags) or (field in field_names):
            toAdd = field
        else:
            toAdd = col
        if toAdd not in clean_columns:
            clean_columns.append(toAdd)
            clean_series.append(df[col])

    clean_df = pd.DataFrame(dict(zip(clean_columns, clean_series)))
    for col in clean_df.columns:
        if col in tags:
            if clean_df.dtypes[col] == object:
                clean_df[col] = clean_df[col].str.lower()

    return clean_df  # .fillna(np.nan).replace('', np.nan)


# @dcache_new.cached
def get_analysis_df(case_query, control_query, modifier_query):
    # NOTE: would be more efficient to select only required data
    df = get_full_df()
    # df = db(Sample_Tag_View).select(processor=pandas_processor)
    # print 'queries', case_query, control_query, modifier_query
    case_df = df.query(case_query.lower())
    # print 'case_df', len(case_df)
    control_df = df.query(control_query.lower())
    # print 'control_df', len(control_df)
    modifier_df = pd.DataFrame()
    # R: modify then select case and control, no need for this fancy intersections.
    if modifier_query:
        modifier_df = df.query(modifier_query.lower())
        case_df = df.ix[set(case_df.index).intersection(set(modifier_df.index))]
        control_df = df.ix[set(control_df.index).intersection(set(modifier_df.index))]

    # set 0 and 1 for analysis
    overlap_df = df.ix[set(case_df.index).intersection(set(control_df.index))]

    df['sample_class'] = None
    df['sample_class'].ix[case_df.index] = 1
    df['sample_class'].ix[control_df.index] = 0
    df['sample_class'].ix[overlap_df.index] = -1

    analysis_df = df.dropna(subset=["sample_class"])
    return analysis_df


@log_durations(logger.debug)
def load_gse(df, series_id):
    gse_name = series_gse_name(series_id)
    logger.debug('Loading data for %s, id = %d', gse_name, series_id)
    gpl2data = {}
    gpl2probes = {}

    for platform_id in df.query("""series_id == %s""" % series_id).platform_id.unique():
        gpl_name = platform_gpl_name(platform_id)
        gpl2data[gpl_name] = get_data(series_id, platform_id)
        gpl2probes[gpl_name] = get_probes(platform_id)
    samples = df.query('series_id == %s' % series_id)
    return Gse(gse_name, samples, gpl2data, gpl2probes)


@make_lookuper
def series_gse_name():
    return Series.objects.values_list('id', 'gse_name')

@make_lookuper
def platform_gpl_name():
    return Platform.objects.values_list('id', 'gpl_name')


def __getMatrixNumHeaderLines(inStream):
    p = re.compile(r'^"ID_REF"')
    for i, line in enumerate(inStream):
        if p.search(line):
            return i


def matrix_filenames(series_id, platform_id):
    gse_name = series_gse_name(series_id)
    yield "%s/%s_series_matrix.txt.gz" % (gse_name, gse_name)

    gpl_name = platform_gpl_name(platform_id)
    yield "%s/%s-%s_series_matrix.txt.gz" % (gse_name, gse_name, gpl_name)


def get_matrix_filename(series_id, platform_id):
    filenames = list(matrix_filenames(series_id, platform_id))
    mirror_filenames = (os.path.join(SERIES_MATRIX_MIRROR, filename) for filename in filenames)
    mirror_filename = first(filename for filename in mirror_filenames if os.path.isfile(filename))
    if mirror_filename:
        return mirror_filename

    for filename in filenames:
        print 'Loading URL', SERIES_MATRIX_URL + filename, '...'
        try:
            res = urllib2.urlopen(SERIES_MATRIX_URL + filename)
        except urllib2.URLError:
            pass
        else:
            mirror_filename = os.path.join(SERIES_MATRIX_MIRROR, filename)
            print 'Cache to', mirror_filename

            directory = os.path.dirname(mirror_filename)
            if not os.path.exists(directory):
                os.makedirs(directory)
            with open(mirror_filename, 'wb') as f:
                shutil.copyfileobj(res, f)

            return mirror_filename

    raise LookupError("Can't find matrix file for series %s, platform %s"
                      % (series_id, platform_id))


@log_durations(logger.debug)
def get_data(series_id, platform_id):
    matrixFilename = get_matrix_filename(series_id, platform_id)
    # setup data for specific platform
    for attempt in (0, 1):
        try:
            headerRows = __getMatrixNumHeaderLines(gzip.open(matrixFilename))
            na_values = ["null", "NA", "NaN", "N/A", "na", "n/a"]
            data = pd.io.parsers.read_table(gzip.open(matrixFilename),
                                            skiprows=headerRows,
                                            index_col=["ID_REF"],
                                            na_values=na_values,
                                            skipfooter=1,
                                            engine='python')
        except IOError as e:
            # In case we have cirrupt file
            print "Failed loading %s: %s" % (matrixFilename, e)
            os.remove(matrixFilename)
            if attempt:
                raise
            matrixFilename = get_matrix_filename(series_id, platform_id)

    data.index = data.index.astype(str)
    data.index.name = "probe"
    for column in data.columns:
        data[column] = data[column].astype(np.float64)
    # return data.head(100)
    return data


@log_durations(logger.debug)
def get_probes(platform_id):
    df = PlatformProbe.objects.filter(platform=platform_id).order_by('id').to_dataframe()
    # df = db(Platform_Probe.platform_id == platform_id).select(processor=pandas_processor)
    df.columns = [col.lower().replace("platform_probe.", "") for col in df.columns]
    df.probe = df.probe.astype(str)  # must cast probes as str
    df = df.set_index('probe')
    return df


class Gse:
    def __init__(self, name, samples, gpl2data, gpl2probes):
        self.name = name
        self.samples = samples
        self.gpl2data = gpl2data
        self.gpl2probes = gpl2probes

    def __str__(self):
        return '<Gse %r>' % self.name


# @dcache.checked(strict=True, subs={
#     'probe dataMu dataSi*.debug=HIV resistacnce.5ed75c5ee5401c45ab16b6555aab5d2b':
#         'probe dataMu dataSi*.debug=HIV resistacnce.6a5171890c81e9cc7fbc5b2f5c2b7a9d'
# })
# @dcache_new.cached
def getFullMetaAnalysis(fcResults, debug=False):
    debug and fcResults.to_csv("%s.fc.csv" % debug)
    all = []
    # i = 0
    allGeneEstimates = fcResults.sort('p') \
        .drop_duplicates(['gse', 'gpl', 'mygene_sym', 'mygene_entrez', 'subset']) \
        .dropna()
    debug and allGeneEstimates.to_csv("%s.geneestimates.csv" % debug)
    for group, geneEstimates in allGeneEstimates.groupby(['mygene_sym', 'mygene_entrez']):
        mygene_sym, mygene_entrez = group
        # if debug:
        #     print i, group
        # i += 1
        # if i > 10:
        #     break
        metaAnalysis = getMetaAnalysis(geneEstimates)
        metaAnalysis['caseDataCount'] = geneEstimates['caseDataCount'].sum()
        metaAnalysis['controlDataCount'] = geneEstimates['controlDataCount'].sum()
        metaAnalysis['mygene_sym'] = mygene_sym
        metaAnalysis['mygene_entrez'] = mygene_entrez
        all.append(metaAnalysis)

    allMetaAnalysis = pd.DataFrame(all).set_index(['mygene_sym', 'mygene_entrez'])
    debug and allMetaAnalysis.to_csv('allMetaAnalysis.csv')
    allMetaAnalysis['direction'] = allMetaAnalysis['random_TE'].map(
        lambda x: "up" if x >= 0 else "down")

    return allMetaAnalysis


# @dcache_new.cached
def getMetaAnalysis(geneEstimates):
    return MetaAnalyser(geneEstimates).get_results()


class GseAnalyzer:
    def __init__(self, gse):
        self.gse = gse

    def getResults(self, debug=False):
        gse = self.gse
        samples = gse.samples

        if 'subset' not in samples.columns:
            samples['subset'] = "NA"

        groups = samples.ix[samples.sample_class >= 0] \
            .groupby(['subset', 'gpl_name'])

        allResults = pd.DataFrame()

        for group, df in groups:
            subset, gpl = group
            probes = gse.gpl2probes[gpl]
            print subset, gpl

            # NOTE: if data has changed then sample ids could be different
            if not set(df["gsm_name"]) <= set(gse.gpl2data[gpl].columns):
                print "skipping %s: sample ids mismatch" % gpl
                continue

            df = df.set_index("gsm_name")
            data = gse.gpl2data[gpl][df.index]
            # Drop samples with > 80% missing samples
            # data = data.dropna(axis=1, thresh=data.shape[0] * .2)

            myCols = ['mygene_sym', 'mygene_entrez']
            table = pd.DataFrame(columns=myCols).set_index(myCols)
            # Studies with defined SAMPLE CLASS
            # at least 2 samples required
            if len(df.sample_class) < 3:
                print "skipping for insufficient data", df.sample_class
                continue
            # at least 1 case and control required
            classes = df.sample_class.unique()
            if not (0 in classes and 1 in classes):
                print "skipping for insufficient data", df.sample_class
                continue
            # data.to_csv("data.test.csv")
            sample_class = df.ix[data.columns].sample_class

            debug = debug and debug + ".%s_%s_%s" % (self.gse.name, gpl, subset)
            table = getFoldChangeAnalysis(data, sample_class,
                                          debug=debug)
            debug and table.to_csv("%s.table.csv" % debug)

            if not table.empty:
                table['direction'] = table.log2foldChange.map(
                    lambda x: "up" if x > 0 else 'down')
                table['subset'] = subset
                table['gpl'] = gpl
                table['gse'] = self.gse.name
                probes = gse.gpl2probes[gpl]
                table = table \
                    .join(probes[['mygene_entrez', 'mygene_sym']]) \
                    .dropna(subset=['mygene_entrez', 'mygene_sym'])
                allResults = pd.concat([allResults, table.reset_index()])
        # allResults.index.name = "probe"
        self.results = allResults
        return allResults


class MetaAnalyser:
    def isquared(self, Q, df, level):
        """
        Calculate I-Squared.
        Higgins & Thompson (2002), Statistics in Medicine, 21, 1539-58.
        """
        tres = self.calcH(Q, df, level)
        result = EasyDict(TE=None, lower=None, upper=None)
        if tres.TE:
            t = lambda x: (x ** 2 - 1) / x ** 2
            result = EasyDict(TE=t(tres.TE), lower=t(tres.lower), upper=t(tres.upper))
        return result

    def calcH(self, Q, df, level):
        """
        Calculate H.
        Higgins & Thompson (2002), Statistics in Medicine, 21, 1539-58.
        """
        k = df + 1
        H = np.sqrt(Q / (k - 1))

        result = EasyDict(TE=None, lower=None, upper=None)
        if k > 2:
            if Q <= k:
                selogH = np.sqrt(1 / (2 * (k - 2)) * (1 - 1 / (3 * (k - 2) ** 2)))
            else:
                selogH = 0.5 * (np.log(Q) - np.log(k - 1)) / (np.sqrt(2 * Q) - np.sqrt(2 * k - 3))

            tres = self.getConfidenceIntervals(np.log(H), selogH, level)
            result = EasyDict(TE=1 if np.exp(tres.TE) < 1 else np.exp(tres.TE),
                              lower=1 if np.exp(tres.lower) < 1 else np.exp(tres.lower),
                              upper=1 if np.exp(tres.upper) < 1 else np.exp(tres.upper))
        return result

    def getConfidenceIntervals(self, TE, TE_se, level=.95, df=None):
        import scipy.stats as stats

        alpha = 1 - level
        # print TE, TE_se
        zscore = TE / TE_se
        if not df:
            lower = TE - stats.norm.ppf(1 - alpha / 2) * TE_se
            upper = TE + stats.norm.ppf(1 - alpha / 2) * TE_se
            pval = 2 * (1 - stats.norm.cdf(abs(zscore)))
        else:
            lower = TE - stats.t.ppf(1 - alpha / 2, df=df) * TE_se
            upper = TE + stats.t.ppf(1 - alpha / 2, df=df) * TE_se
            pval = 2 * (1 - stats.t.cdf(abs(zscore), df=df))

        result = EasyDict(TE=TE,
                          se=TE_se,
                          level=level,
                          df=df,
                          pval=pval,
                          zscore=zscore,
                          upper=upper,
                          lower=lower)

#         if isinstance(TE_se, collections.Iterable):
#             result = pd.DataFrame(result)
        return result

    def __init__(self, gene_stats):
        TE = gene_stats.caseDataMu - gene_stats.controlDataMu

        # (7) Calculate results for individual studies
        # MD method
        TE_se = np.sqrt(
            gene_stats['caseDataSigma'] ** 2 / gene_stats['caseDataCount']
            + gene_stats['controlDataSigma'] ** 2 / gene_stats['controlDataCount']
        )
        # Studies with non-positive variance get zero weight in meta-analysis
        TE_se[(gene_stats['caseDataSigma'] <= 0) | (gene_stats['controlDataSigma'] <= 0)] = None
        w_fixed = (1 / TE_se ** 2).fillna(0)

        TE_fixed = np.average(TE, weights=w_fixed)
        TE_fixed_se = np.sqrt(1 / sum(w_fixed))
        self.fixed = self.getConfidenceIntervals(TE_fixed, TE_fixed_se)

        self.Q = sum(w_fixed * (TE - TE_fixed) ** 2)
        self.Q_df = TE_se.dropna().count() - 1

        self.cVal = sum(w_fixed) - sum(w_fixed ** 2) / sum(w_fixed)
        if self.Q <= self.Q_df:
            self.tau2 = 0
        else:
            self.tau2 = (self.Q - self.Q_df) / self.cVal
        self.tau = np.sqrt(self.tau2)
        self.tau2_se = None
        w_random = (1 / (TE_se ** 2 + self.tau2)).fillna(0)
        TE_random = np.average(TE, weights=w_random)
        TE_random_se = np.sqrt(1 / sum(w_random))
        self.random = self.getConfidenceIntervals(TE_random, TE_random_se)

        # Prediction interval
        self.level_predict = 0.95
        self.k = TE_se.count()
        self.predict = EasyDict(TE=None,
                                se=None,
                                level=None,
                                df=None,
                                pval=None,
                                zscore=None,
                                upper=None,
                                lower=None)
        if self.k >= 3:
            TE_predict_se = np.sqrt(TE_random_se ** 2 + self.tau2)
            self.predict = self.getConfidenceIntervals(TE_random, TE_predict_se, self.level_predict,
                                                       self.k - 2)

        # Calculate H and I-Squared
        self.level_comb = 0.95
        self.H = self.calcH(self.Q, self.Q_df, self.level_comb)
        self.I2 = self.isquared(self.Q, self.Q_df, self.level_comb)

    def get_results(self):
        return dict(
            k=self.k,
            fixed_TE=self.fixed.TE,
            fixed_se=self.fixed.se,
            fixed_lower=self.fixed.lower,
            fixed_upper=self.fixed.upper,
            fixed_pval=self.fixed.pval,
            fixed_zscore=self.fixed.zscore,

            random_TE=self.random.TE,
            random_se=self.random.se,
            random_lower=self.random.lower,
            random_upper=self.random.upper,
            random_pval=self.random.pval,
            random_zscore=self.random.zscore,


            predict_TE=self.predict.TE,
            predict_se=self.predict.se,
            predict_lower=self.predict.lower,
            predict_upper=self.predict.upper,
            predict_pval=self.predict.pval,
            predict_zscore=self.predict.zscore,

            tau2=self.tau2,
            tau2_se=self.tau2_se,

            C=self.cVal,

            H=self.H.TE,
            H_lower=self.H.lower,
            H_upper=self.H.upper,

            I2=self.I2.TE,
            I2_lower=self.I2.lower,
            I2_upper=self.I2.upper,

            Q=self.Q,
            Q_df=self.Q_df,
        )


def getFoldChangeAnalysis(data, sample_class, debug=False):
    from scipy.stats import ttest_ind

    data = normalize_quantiles(get_logged(data))

    summary = pd.DataFrame(index=data.index)

    summary['dataMu'] = data.mean(axis="columns")
    summary['dataSigma'] = data.std(axis="columns")
    summary['dataCount'] = data.count(axis="columns")

    caseData = data.T[sample_class == 1].T
    debug and caseData.to_csv("%s.case.data.csv" % debug)
    summary['caseDataMu'] = caseData.mean(axis="columns")
    summary['caseDataSigma'] = caseData.std(axis="columns") if len(caseData.columns) > 1 else 0
    summary['caseDataCount'] = caseData.count(axis="columns")

    controlData = data.T[sample_class == 0].T
    debug and controlData.to_csv("%s.control.data.csv" % debug)

    summary['controlDataMu'] = controlData.mean(axis="columns")
    summary['controlDataSigma'] = controlData.std(axis="columns") \
        if len(controlData.columns) > 1 else 0
    summary['controlDataCount'] = controlData.count(axis="columns")

    summary['fc'] = summary['caseDataMu'] - summary['controlDataMu']
    summary['log2foldChange'] = summary['fc']
    # else:
    # summary['fc'] = np.log2(summary['caseDataMu']/summary['controlDataMu'])

    summary['effect_size'] = summary['fc'] / summary['dataSigma']

    ttest, prob = ttest_ind(caseData, controlData, axis=1)
    summary['ttest'] = ttest
    summary['p'] = prob
    summary['direction'] = summary['effect_size'].map(lambda x: "up" if x >= 0 else "down")

    return summary


def normalize_quantiles(df):
    """
    df with samples in the columns and probes across the rows
    """
    # http://biopython.org/pipermail/biopython/2010-March/006319.html
    A = df.values
    AA = np.empty_like(A)
    I = np.argsort(A, axis=0)
    AA[I, np.arange(A.shape[1])] = np.mean(A[I, np.arange(A.shape[1])], axis=1)[:, np.newaxis]
    return pd.DataFrame(AA, index=df.index, columns=df.columns)


import numexpr as ne

def get_logged(data):
    if is_logged(data):
        return data
    floor = np.abs(np.min(data))
    res = ne.evaluate('log(data + floor + 1) / log(2)')
    return pd.DataFrame(res, index=data.index, columns=data.columns)

def is_logged(data):
    return ne.evaluate('data < 10').all().all()