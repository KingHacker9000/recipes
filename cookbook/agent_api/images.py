import base64
import binascii
import io
import uuid
import warnings

from django.core.files.base import ContentFile
from PIL import Image, UnidentifiedImageError


MAX_IMAGE_BYTES = 8 * 1024 * 1024
MAX_IMAGE_PIXELS = 20_000_000
ALLOWED_FORMATS = {
    'JPEG': ('jpg', 'image/jpeg'),
    'PNG': ('png', 'image/png'),
    'WEBP': ('webp', 'image/webp'),
}


class AgentRecipeImageError(ValueError):
    pass


def _decode_base64(value):
    if not isinstance(value, str) or not value.strip():
        raise AgentRecipeImageError('image_base64 is required.')
    encoded = value.strip()
    if encoded.lower().startswith('data:'):
        if ',' not in encoded:
            raise AgentRecipeImageError('Invalid image data URI.')
        encoded = encoded.split(',', 1)[1]
    try:
        raw = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError):
        raise AgentRecipeImageError('image_base64 is not valid base64.')
    if not raw:
        raise AgentRecipeImageError('Decoded image is empty.')
    if len(raw) > MAX_IMAGE_BYTES:
        raise AgentRecipeImageError('Recipe image exceeds the 8 MiB upload limit.')
    return raw


def normalize_recipe_image(image_base64, *, content_type=''):
    """Validate and re-encode a recipe image before it enters media storage.

    Re-encoding strips metadata and prevents arbitrary bytes from being stored
    under an image extension. Only JPEG, PNG and WEBP are accepted.
    """
    raw = _decode_base64(image_base64)
    try:
        with warnings.catch_warnings():
            warnings.simplefilter('error', Image.DecompressionBombWarning)
            image = Image.open(io.BytesIO(raw))
            image.load()
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombWarning):
        raise AgentRecipeImageError('Decoded payload is not a valid supported image.')

    image_format = str(image.format or '').upper()
    if image_format not in ALLOWED_FORMATS:
        raise AgentRecipeImageError('Only JPEG, PNG and WEBP recipe images are supported.')

    width, height = image.size
    if width <= 0 or height <= 0 or width * height > MAX_IMAGE_PIXELS:
        raise AgentRecipeImageError('Recipe image exceeds the 20 megapixel limit.')

    extension, canonical_content_type = ALLOWED_FORMATS[image_format]
    supplied_content_type = str(content_type or '').strip().lower()
    if supplied_content_type and supplied_content_type != canonical_content_type:
        raise AgentRecipeImageError(
            f'content_type does not match the decoded {image_format} image.'
        )

    output = io.BytesIO()
    if image_format == 'JPEG':
        image.convert('RGB').save(output, 'JPEG', quality=90, optimize=True)
    elif image_format == 'PNG':
        image.save(output, 'PNG', optimize=True)
    else:
        image.save(output, 'WEBP', quality=90, method=6)

    normalized = output.getvalue()
    if len(normalized) > MAX_IMAGE_BYTES:
        raise AgentRecipeImageError('Normalized recipe image exceeds the 8 MiB upload limit.')

    return {
        'content': ContentFile(normalized),
        'filename': f'{uuid.uuid4().hex}.{extension}',
        'content_type': canonical_content_type,
        'width': width,
        'height': height,
        'size_bytes': len(normalized),
    }


def recipe_image_payload(recipe):
    if not recipe.image:
        return {
            'recipe_id': recipe.id,
            'image': None,
            'updated_at': recipe.updated_at.isoformat() if recipe.updated_at else None,
        }
    try:
        url = recipe.image.url
    except (ValueError, OSError):
        url = None
    return {
        'recipe_id': recipe.id,
        'image': {
            'name': recipe.image.name,
            'url': url,
        },
        'updated_at': recipe.updated_at.isoformat() if recipe.updated_at else None,
    }


def save_recipe_image(recipe, image_base64, *, content_type=''):
    normalized = normalize_recipe_image(image_base64, content_type=content_type)
    old_name = recipe.image.name if recipe.image else ''
    storage = recipe.image.storage

    recipe.image.save(
        normalized['filename'],
        normalized['content'],
        save=True,
    )

    if old_name and old_name != recipe.image.name:
        try:
            storage.delete(old_name)
        except OSError:
            pass

    return {
        **recipe_image_payload(recipe),
        'content_type': normalized['content_type'],
        'width': normalized['width'],
        'height': normalized['height'],
        'size_bytes': normalized['size_bytes'],
    }
