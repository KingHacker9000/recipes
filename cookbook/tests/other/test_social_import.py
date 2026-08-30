import pytest

from cookbook.social_import.acquisition import (
    SocialImportError,
    canonicalize_url,
    extract_external_id,
    identify_platform,
)
from cookbook.social_import.service import normalize_extraction


@pytest.mark.parametrize(
    ('url', 'platform'),
    [
        ('https://www.tiktok.com/@chef/video/1234567890', 'tiktok'),
        ('https://www.instagram.com/reel/ABC123/?igshid=x', 'instagram'),
        ('https://www.youtube.com/shorts/abcdEFG?si=tracking', 'youtube'),
        ('https://youtu.be/abcdEFG?t=12', 'youtube'),
    ],
)
def test_identify_supported_platforms(url, platform):
    assert identify_platform(url) == platform


@pytest.mark.parametrize(
    'url',
    [
        'file:///etc/passwd',
        'https://www.tiktok.com.evil.example/video/1',
        'https://user:pass@www.instagram.com/reel/ABC/',
        'https://example.com/reel/ABC/',
    ],
)
def test_rejects_unsafe_or_unrelated_urls(url):
    with pytest.raises(SocialImportError):
        identify_platform(url)


def test_canonicalize_removes_tracking_and_fragment():
    value = canonicalize_url(
        'http://m.youtube.com/shorts/abc123?utm_source=x&si=y&feature=share#fragment'
    )
    assert value == 'https://www.youtube.com/shorts/abc123?feature=share'


def test_external_ids():
    assert extract_external_id('tiktok', 'https://www.tiktok.com/@chef/video/12345') == '12345'
    assert extract_external_id('instagram', 'https://www.instagram.com/reel/ABC123/') == 'ABC123'
    assert extract_external_id('youtube', 'https://www.youtube.com/shorts/xyz987') == 'xyz987'


def test_normalize_extraction_keeps_uncertainty_explicit():
    value = normalize_extraction({
        'title': 'Pasta',
        'servings': 4,
        'confidence': 1.7,
        'ingredients': [
            {'food': 'chicken', 'quantity': 500, 'unit': 'g', 'confidence': 0.95, 'source': 'video_text'},
            {'food': 'cheese', 'quantity': 'some', 'confidence': -1, 'source': 'caption'},
            {'food': ''},
        ],
        'steps': ['Cook it'],
    })
    assert value['confidence'] == 1.0
    assert value['ingredients'][1]['quantity'] is None
    assert value['ingredients'][1]['confidence'] == 0.0
    assert len(value['ingredients']) == 2
    assert value['steps'][0]['text'] == 'Cook it'
