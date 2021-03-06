from funcy import all, walk_keys

from rest_framework import serializers

from legacy.models import Platform, Series, Analysis, MetaAnalysis, Sample
from tags.models import SeriesAnnotation, Tag

from .fields import S3Field


class SeriesSerializer(serializers.ModelSerializer):
    attrs = serializers.JSONField()

    class Meta:
        model = Series
        fields = '__all__'


class AnalysisSerializer(serializers.ModelSerializer):
    df = S3Field()
    fold_changes = S3Field()

    class Meta:
        model = Analysis
        exclude = ['created_by', 'modified_by', 'is_active',
                   'created_on', 'modified_on', ]


class AnalysisParamSerializer(serializers.ModelSerializer):
    class Meta:
        model = Analysis
        fields = ['specie', 'case_query',
                  'control_query', 'modifier_query', ]

    def __init__(self, *args, **kwargs):
        super(AnalysisParamSerializer, self).__init__(*args, **kwargs)
        self.fields['specie'].required = True
        self.fields['specie'].allow_blank = False


class SeriesAnnotationSerializer(serializers.ModelSerializer):
    class Meta:
        model = SeriesAnnotation
        exclude = ['series_tag', 'created_on', 'modified_on', ]


class SampleAnnotationValidator(serializers.Serializer):
    tag = serializers.PrimaryKeyRelatedField(queryset=Tag.objects.all())
    series = serializers.SlugRelatedField(queryset=Series.objects.all(), slug_field='gse_name')
    platform = serializers.SlugRelatedField(queryset=Platform.objects.all(), slug_field='gpl_name')
    note = serializers.CharField(required=False, default='')
    annotations = serializers.JSONField()

    def validate_annotations(self, annotations):
        if not all(isinstance(v, str) for v in list(annotations.values())):
            raise serializers.ValidationError("Annotations should be a dict of GSMs -> tag values")

        if not all(isinstance(v, (str, int)) for v in list(annotations.keys())):
                raise serializers.ValidationError(
                    "Annotations should be a dict of GSMs -> tag values")

        return annotations

    def validate(self, data):
        gsm_to_id = dict(data['series'].samples.filter(
            platform=data['platform']).values_list('gsm_name', 'id'))

        all_samples = set(gsm_to_id)

        tagged_samples = set(data['annotations'])

        missing_annotations = all_samples - tagged_samples
        if missing_annotations:
            raise serializers.ValidationError(
                ["There are samples with ids {0} which are missing their annotation"
                 .format(missing_annotations)
                 ])

        extra_annotations = tagged_samples - all_samples
        if extra_annotations:
            raise serializers.ValidationError(
                ["There is samples with id {0} which doesn't belongs to series {1}"
                 .format(extra_annotations, data['series'].id)
                 ])

        data['annotations'] = walk_keys(gsm_to_id, data['annotations'])
        return data

class SampleSerializer(serializers.ModelSerializer):
    series = serializers.SlugRelatedField(slug_field='gse_name', read_only=True)
    platform = serializers.SlugRelatedField(slug_field='gpl_name', read_only=True)
    attrs = serializers.JSONField()

    class Meta:
        model = Sample
        fields = ['series', 'platform', 'attrs', 'gsm_name']

class TagSerializer(serializers.ModelSerializer):
    class Meta:
        model = Tag
        exclude = ['created_by', 'modified_by', 'is_active',
                   'created_on', 'modified_on', ]

class MetaAnalysisSerializer(serializers.ModelSerializer):
    class Meta:
        model = MetaAnalysis
        fields = '__all__'


class PlatformSerializer(serializers.ModelSerializer):
    class Meta:
        model = Platform
        exclude = ('stats', 'history',
                   'verdict', 'last_filled', )
