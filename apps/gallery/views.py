import imghdr
import json
import logging

from django.conf import settings
from django.db.models import Q
from django.http import (HttpResponse, HttpResponseRedirect,
                         HttpResponseBadRequest, Http404)
from django.shortcuts import get_object_or_404
from django.views.decorators.http import require_POST

from commonware.decorators import xframe_sameorigin
import jingo
from tower import ugettext_lazy as _lazy

from access.decorators import login_required
from gallery import ITEMS_PER_PAGE
from gallery.forms import ImageForm, VideoForm, UploadTypeForm
from gallery.models import Image, Video
from gallery.utils import upload_image, upload_video, check_media_permissions
from sumo.urlresolvers import reverse
from sumo.utils import paginate
from upload.tasks import generate_thumbnail
from upload.utils import FileTooLargeError
from wiki.tasks import schedule_rebuild_kb

MSG_FAIL_UPLOAD = {'image': _lazy(u'Could not upload your image.'),
                   'video': _lazy(u'Could not upload your video.')}


log = logging.getLogger('k.gallery')


def gallery(request, media_type='image'):
    """The media gallery.

    Filter can be set to 'images' or 'videos'.

    """
    if media_type == 'image':
        media_qs = Image.objects.filter(locale=request.locale)
    elif media_type == 'video':
        media_qs = Video.objects.filter(locale=request.locale)
    else:
        raise Http404

    media = paginate(request, media_qs, per_page=ITEMS_PER_PAGE)

    drafts = _get_drafts(request.user)
    image_form, video_form, upload_type_form = _init_forms(request, drafts)

    return jingo.render(request, 'gallery/gallery.html',
                        {'media': media,
                         'media_type': media_type,
                         'upload_type_form': upload_type_form,
                         'image_form': image_form,
                         'video_form': video_form})


@login_required
@require_POST
def upload(request, media_type='image'):
    """Finalizes an uploaded draft."""
    drafts = _get_drafts(request.user)
    if media_type == 'image' and drafts['image']:
        # We're publishing an image draft!
        image_form = _init_media_form(ImageForm, request, drafts['image'][0])
        if image_form.is_valid():
            img = image_form.save(is_draft=None)
            generate_thumbnail.delay(img, 'file', 'thumbnail')
            # TODO: We can drop this when we start using Redis.
            invalidate = Image.objects.exclude(pk=img.pk)
            if invalidate.exists():
                Image.objects.invalidate(invalidate[0])
            # Rebuild KB
            schedule_rebuild_kb()
            return HttpResponseRedirect(img.get_absolute_url())
        else:
            return gallery(request, media_type='image')
    elif media_type == 'video' and drafts['video']:
        # We're publishing a video draft!
        video_form = _init_media_form(VideoForm, request, drafts['video'][0])
        if video_form.is_valid():
            vid = video_form.save(is_draft=None)
            if vid.thumbnail:
                generate_thumbnail.delay(vid, 'poster', 'poster',
                                         max_size=settings.WIKI_VIDEO_WIDTH)
                generate_thumbnail.delay(vid, 'thumbnail', 'thumbnail')
            # TODO: We can drop this when we start using Redis.
            invalidate = Video.objects.exclude(pk=vid.pk)
            if invalidate.exists():
                Video.objects.invalidate(invalidate[0])
            # Rebuild KB
            schedule_rebuild_kb()
            return HttpResponseRedirect(vid.get_absolute_url())
        else:
            return gallery(request, media_type='video')

    return HttpResponseBadRequest(u'Unrecognized POST request.')


@login_required
@require_POST
def cancel_draft(request, media_type='image'):
    """Delete an existing draft for the user."""
    drafts = _get_drafts(request.user)
    if media_type == 'image' and drafts['image']:
        drafts['image'].delete()
        drafts['image'] = None
    elif media_type == 'video' and drafts['video']:
        delete_file = request.GET.get('field')
        if delete_file not in ('flv', 'ogv', 'webm', 'thumbnail'):
            delete_file = None

        if delete_file and getattr(drafts['video'][0], delete_file):
            getattr(drafts['video'][0], delete_file).delete()
            if delete_file == 'thumbnail' and drafts['video'][0].poster:
                drafts['video'][0].poster.delete()
        elif not delete_file:
            drafts['video'].delete()
            drafts['video'] = None
    else:
        msg = u'Unrecognized request or nothing to cancel.'
        mimetype = None
        if request.is_ajax():
            msg = json.dumps({'status': 'error', 'message': msg})
            mimetype = 'application/json'
        return HttpResponseBadRequest(msg, mimetype=mimetype)

    if request.is_ajax():
        return HttpResponse(json.dumps({'status': 'success'}),
                            mimetype='application/json')

    return HttpResponseRedirect(reverse('gallery.gallery', args=[media_type]))


def gallery_async(request):
    """AJAX endpoint to media gallery.

    Returns an HTML list representation of the media.

    """
    # Maybe refactor this into existing views and check request.is_ajax?
    media_type = request.GET.get('type', 'image')
    term = request.GET.get('q')
    if media_type == 'image':
        media_qs = Image.objects
    elif media_type == 'video':
        media_qs = Video.objects
    else:
        raise Http404

    if request.locale == settings.WIKI_DEFAULT_LANGUAGE:
        media_qs = media_qs.filter(locale=request.locale)
    else:
        locales = [request.locale, settings.WIKI_DEFAULT_LANGUAGE]
        media_qs = media_qs.filter(locale__in=locales)

    if term:
        media_qs = media_qs.filter(Q(title__icontains=term) |
                                   Q(description__icontains=term))

    media = paginate(request, media_qs, per_page=ITEMS_PER_PAGE)

    return jingo.render(request, 'gallery/includes/media_list.html',
                        {'media_list': media})


def search(request, media_type):
    """Search the media gallery."""

    term = request.GET.get('q')
    if not term:
        url = reverse('gallery.gallery', args=[media_type])
        return HttpResponseRedirect(url)

    filter = Q(title__icontains=term) | Q(description__icontains=term)

    if media_type == 'image':
        media_qs = Image.objects.filter(filter, locale=request.locale)
    elif media_type == 'video':
        media_qs = Video.objects.filter(filter, locale=request.locale)
    else:
        raise Http404

    media = paginate(request, media_qs, per_page=ITEMS_PER_PAGE)

    return jingo.render(request, 'gallery/search.html',
                        {'media': media,
                         'media_type': media_type,
                         'q': term})


@login_required
def delete_media(request, media_id, media_type='image'):
    """Delete media and redirect to gallery view."""
    media, media_format = _get_media_info(media_id, media_type)

    check_media_permissions(media, request.user, 'delete')

    if request.method == 'GET':
        # Render the confirmation page
        return jingo.render(request, 'gallery/confirm_media_delete.html',
                            {'media': media, 'media_type': media_type,
                             'media_format': media_format})

    # Handle confirm delete form POST
    log.warning('User %s is deleting %s with id=%s' %
                (request.user, media_type, media.id))
    media.delete()
    # Rebuild KB
    schedule_rebuild_kb()
    return HttpResponseRedirect(reverse('gallery.gallery', args=[media_type]))


@login_required
def edit_media(request, media_id, media_type='image'):
    """Edit media means only changing the description, for now."""
    media, media_format = _get_media_info(media_id, media_type)

    check_media_permissions(media, request.user, 'change')

    if media_type == 'image':
        media_form = _init_media_form(ImageForm, request, media,
                                      ('locale', 'title'))
    else:
        media_form = _init_media_form(VideoForm, request, media,
                                      ('locale', 'title'))

    if request.method == 'POST' and media_form.is_valid():
        media = media_form.save(update_user=request.user)
        return HttpResponseRedirect(
            reverse('gallery.media', args=[media_type, media_id]))

    return jingo.render(request, 'gallery/edit_media.html',
                        {'media': media,
                         'media_format': media_format,
                         'form': media_form,
                         'media_type': media_type})


def media(request, media_id, media_type='image'):
    """The media page."""
    media, media_format = _get_media_info(media_id, media_type)
    return jingo.render(request, 'gallery/media.html',
                        {'media': media,
                         'media_format': media_format,
                         'media_type': media_type})


@login_required
@require_POST
@xframe_sameorigin
def upload_async(request, media_type='image'):
    """Upload images or videos from request.FILES."""
    # TODO(paul): validate the Submit File on upload modal async
    #             even better, use JS validation for title length.
    try:
        if media_type == 'image':
            file_info = upload_image(request)
        else:
            file_info = upload_video(request)
    except FileTooLargeError as e:
        return HttpResponseBadRequest(
            json.dumps({'status': 'error', 'message': e.args[0]}))

    if isinstance(file_info, dict) and 'thumbnail_url' in file_info:
        schedule_rebuild_kb()
        return HttpResponse(
            json.dumps({'status': 'success', 'file': file_info}))

    message = MSG_FAIL_UPLOAD[media_type]
    return HttpResponseBadRequest(
        json.dumps({'status': 'error', 'message': unicode(message),
                    'errors': file_info}))


def _get_media_info(media_id, media_type):
    """Returns an image or video along with media format for the image."""
    media_format = None
    if media_type == 'image':
        media = get_object_or_404(Image, pk=media_id)
        media_format = imghdr.what(media.file.path)
    elif media_type == 'video':
        media = get_object_or_404(Video, pk=media_id)
    else:
        raise Http404
    return (media, media_format)


def _get_drafts(user):
    """Get video and image drafts for a given user."""
    drafts = {'image': None, 'video': None}
    if user.is_authenticated():
        drafts['image'] = Image.objects.filter(creator=user, is_draft=True)
        drafts['video'] = Video.objects.filter(creator=user, is_draft=True)
    return drafts


def _init_media_form(form_cls, request=None, obj=None,
                     ignore_fields=()):
    """Initializes the media form with an Image/Video instance and POSTed data.

    form_cls is a django ModelForm
    Request method must be POST for POST data to be bound.
    exclude_fields contains the list of fields to default to their current
    value from the Image/Video object.

    """
    post_data = None
    initial = None
    if request:
        initial = {'locale': request.locale}
    file_data = None
    if request.method == 'POST':
        file_data = request.FILES
        post_data = request.POST.copy()
        if obj and ignore_fields:
            for f in ignore_fields:
                post_data[f] = getattr(obj, f)

    return form_cls(post_data, file_data, instance=obj, initial=initial,
                    is_ajax=False)


def _init_forms(request, drafts):
    """Initialize video and image upload forms given the request and drafts."""
    image = drafts['image'][0] if drafts['image'] else None
    image_form = _init_media_form(ImageForm, request, image)
    video = drafts['video'][0] if drafts['video'] else None
    video_form = _init_media_form(VideoForm, request, video)
    upload_type_form = UploadTypeForm()
    if request.method == 'POST':
        image_form.is_valid()
        video_form.is_valid()

    return (image_form, video_form, upload_type_form)
