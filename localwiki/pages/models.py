from urllib import quote
from urllib import unquote_plus
import mimetypes
import re
from urlparse import urljoin
from lxml.html import fragments_fromstring
from copy import copy

from django.contrib.gis.db import models
from django.conf import settings
from django.core.urlresolvers import set_urlconf, get_urlconf
from django.template.defaultfilters import stringfilter
from django.core.exceptions import ValidationError
from django.utils.safestring import mark_safe
from django.utils.translation import ugettext as _
from django.utils.translation import ugettext_lazy

from django_randomfilenamestorage.storage import (
    RandomFilenameFileSystemStorage)

from versionutils import diff, versioning
from versionutils.versioning.utils import is_versioned, unique_lookup_values_for
from localwiki.utils.urlresolvers import reverse
from regions.models import Region

from . import exceptions
from .fields import WikiHTMLField
from .constants import page_base_path


def validate_page_slug(slug):
    if slugify(slug) != slug:
        raise ValidationError(_('Provided slug is invalid. Slugs must be lowercase, '
            'contain no trailing or leading whitespace, and contain only alphanumber '
            'characters along with %(KEEP_CHARACTERS)s') % {'KEEP_CHARACTERS': SLUGIFY_KEEP})


class Page(models.Model):
    name = models.CharField(max_length=255, blank=False)
    slug = models.CharField(max_length=255, editable=False, blank=False, db_index=True,
        validators=[validate_page_slug])
    content = WikiHTMLField()
    region = models.ForeignKey(Region, null=True)

    class Meta:
        unique_together = ('slug', 'region')

    def __unicode__(self):
        return self.name

    def get_absolute_url(self):
        return page_url(self.name, self.region)

    def get_url_for_share(self, request):
        # Want to use a semi-canonical URL here
        if request.host.name == settings.DEFAULT_HOST:
            return self.get_absolute_url()
        else:
            current_urlconf = get_urlconf() or settings.ROOT_URLCONF
            set_urlconf(settings.ROOT_URLCONF)
            url = self.get_absolute_url()
            set_urlconf(current_urlconf)
            return url

    def save(self, *args, **kwargs):
        self.slug = slugify(self.name)
        super(Page, self).save(*args, **kwargs)

    def clean(self):
        self.name = clean_name(self.name)
        if not slugify(self.name):
            raise ValidationError(_('Page name is invalid.'))

    def exists(self):
        """
        Returns:
            True if the Page currently exists in the database.
        """
        return Page.objects.filter(slug=self.slug, region=self.region).exists()

    def is_front_page(self):
        return self.name.lower() == 'front page'

    def is_template_page(self):
        return (self.name.lower().startswith('templates/') or
                self.name.lower() == 'templates')

    def pretty_slug(self):
        if not self.name:
            return self.slug
        return name_to_url(self.name)
    pretty_slug = property(pretty_slug)

    def name_parts(self):
        return self.name.split('/')
    name_parts = property(name_parts)

    def _get_related_objs(self):
        related_objs = []
        for r in self._meta.get_all_related_objects():
            try:
                rel_obj = getattr(self, r.get_accessor_name())
            except:
                continue  # No object for this relation.

            # Is this a related /set/, e.g. redirect_set?
            if isinstance(rel_obj, models.Manager):
                # list() freezes the QuerySet, which we don't want to be
                # fetched /after/ we delete the page.
                related_objs.append(
                    (r.get_accessor_name(), list(rel_obj.all())))
            else:
                related_objs.append((r.get_accessor_name(), rel_obj))
        return related_objs

    def _get_slug_related_objs(self):
        # Right now this is simply hard-coded.
        # TODO: generalize this slug pattern, perhaps with some kind of
        # AttachedSlugField or something.
        pagefiles = PageFile.objects.filter(slug=self.slug, region=self.region)
        return [{
            'objs': pagefiles,
            'unique_together': ('name', 'slug', 'region')
        }]

    def rename_to(self, pagename):
        """
        Renames the page to `pagename`.  Moves related objects around
        accordingly.
        """
        def _get_slug_lookup(unique_together, obj, new_p):
            d = {}
            for field in unique_together:
                d[field] = getattr(obj, field)
            d['slug'] = new_p.slug
            return d

        def _already_exists(obj):
            M = obj.__class__
            unique_vals = unique_lookup_values_for(obj)
            if not unique_vals:
                return False
            return M.objects.filter(**unique_vals).exists()

        from redirects.models import Redirect
        from redirects.exceptions import RedirectToSelf

        if Page(slug=slugify(pagename), region=self.region).exists():
            if slugify(pagename) == self.slug:
                # The slug is the same but we're changing the name.
                old_name = self.name
                self.name = pagename
                self.save(comment=_('Renamed from "%s"') % old_name)
                return
            else:
                raise exceptions.PageExistsError(
                    _("The page '%s' already exists!") % pagename)

        # Copy the current page into the new page, zeroing out the
        # primary key and setting a new name and slug.
        new_p = copy(self)
        new_p.pk = None
        new_p.name = pagename
        new_p.slug = slugify(pagename)
        new_p._in_rename = True
        new_p.save(comment=_('Renamed from "%s"') % self.name)

        # Get all related objects before the original page is deleted.
        related_objs = self._get_related_objs()

        # Cache all ManyToMany values on related objects so we can restore them
        # later--otherwise they will be lost when page is deleted.
        for attname, rel_obj_list in related_objs:
            if not isinstance(rel_obj_list, list):
                rel_obj_list = [rel_obj_list]
            for rel_obj in rel_obj_list:
                rel_obj._m2m_values = dict(
                    (f.attname, list(getattr(rel_obj, f.attname).all()))
                    for f in rel_obj._meta.many_to_many)

        # Create a redirect from the starting pagename to the new pagename.
        redirect = Redirect(source=self.slug, destination=new_p, region=self.region)
        # Creating the redirect causes the starting page to be deleted.
        redirect.save()

        # Point each related object to the new page and save the object with a
        # 'was renamed' comment.
        for attname, rel_obj in related_objs:
            if isinstance(rel_obj, list):
                for obj in rel_obj:
                    obj.pk = None  # Reset the primary key before saving.
                    try:
                        getattr(new_p, attname).add(obj)
                        if _already_exists(obj):
                            continue
                        if is_versioned(obj):
                            obj.save(comment=_("Parent page renamed"))
                        else:
                            obj.save()
                        # Restore any m2m fields now that we have a new pk
                        for name, value in obj._m2m_values.items():
                            setattr(obj, name, value)
                    except RedirectToSelf, s:
                        # We don't want to create a redirect to ourself.
                        # This happens during a rename -> rename-back
                        # cycle.
                        continue
            else:
                # This is an easy way to set obj to point to new_p.
                setattr(new_p, attname, rel_obj)
                rel_obj.pk = None  # Reset the primary key before saving.
                if _already_exists(rel_obj):
                    continue

                if is_versioned(rel_obj):
                    rel_obj.save(comment=_("Parent page renamed"))
                else:
                    rel_obj.save()
                # Restore any m2m fields now that we have a new pk
                for name, value in rel_obj._m2m_values.items():
                    setattr(rel_obj, name, value)

        # Do the same with related-via-slug objects.
        for info in self._get_slug_related_objs():
            unique_together = info['unique_together']
            objs = info['objs']
            for obj in objs:
                # If we already have the same object with this slug then
                # skip it. This happens when there's, say, a PageFile that's
                # got the same name that's attached to the page -- which can
                # happen during a page rename -> rename back cycle.
                obj_lookup = _get_slug_lookup(unique_together, obj, new_p)
                if obj.__class__.objects.filter(**obj_lookup):
                    continue

                obj.slug = new_p.slug
                obj.pk = None  # Reset the primary key before saving.
                obj.save(comment=_("Parent page renamed"))

        new_p._in_rename = False
        return new_p

    def get_highlight_image(self):
        """
        Return either a good `PageFile` or None if the page
        doesn't contain any images (inside the content).
        """
        from .plugins import _files_url, file_url_to_name

        if not PageFile.objects.filter(slug=self.slug, region=self.region).exists():
            return None

        # Parse the page HTML and look for the first local image
        for e in fragments_fromstring(self.content):
            if isinstance(e, basestring):
                continue
            for i in e.iter('img'):
                src = i.attrib.get('src', '')
                if src.startswith(_files_url):
                    _file = PageFile.objects.filter(
                        slug__exact=self.slug,
                        name__exact=file_url_to_name(src),
                        region=self.region
                    )
                    if _file:
                        return _file[0]


class PageDiff(diff.BaseModelDiff):
    fields = ('name',
              ('content', diff.diffutils.HtmlFieldDiff),
             )


diff.register(Page, PageDiff)
versioning.register(Page)


class PageFile(models.Model):
    file = models.FileField(ugettext_lazy("file"), upload_to='pages/files/',
                            storage=RandomFilenameFileSystemStorage())
    name = models.CharField(max_length=255, blank=False)
    # TODO: Create PageSlugField for this purpose
    slug = models.CharField(max_length=255, blank=False, db_index=True,
        validators=[validate_page_slug])
    region = models.ForeignKey(Region, null=True)

    _rough_type_map = [(r'^audio', 'audio'),
                       (r'^video', 'video'),
                       (r'^application/pdf', 'pdf'),
                       (r'^application/msword', 'word'),
                       (r'^text/html', 'html'),
                       (r'^text', 'text'),
                       (r'^image', 'image'),
                       (r'^application/vnd.ms-powerpoint', 'powerpoint'),
                       (r'^application/vnd.ms-excel', 'excel')
                      ]

    def get_absolute_url(self):
        return reverse('pages:file', kwargs={
            'slug': self.slug,
            'file': self.name,
            'region': self.region
        })

    @property
    def attached_to_page(self):
        try:
            p = Page.objects.get(slug=self.slug, region=self.region)
        except Page.DoesNotExist:
            p = Page(slug=self.slug,
                     name=clean_name(self.slug), region=self.region)
        return p

    @property
    def rough_type(self):
        mime = self.mime_type
        if mime:
            for regex, rough_type in self._rough_type_map:
                if re.match(regex, mime):
                    return rough_type
        return 'unknown'

    @property
    def mime_type(self):
        return mimetypes.guess_type(self.name)[0]

    def is_image(self):
        return self.rough_type == 'image'

    class Meta:
        unique_together = ('slug', 'region', 'name')
        ordering = ['-id']


versioning.register(PageFile)


def clean_name(name):
    # underscores are used to namespace special URLs, so let's remove them
    name = re.sub('_', ' ', name).strip()
    # No pound signs - they're used for anchors
    name = re.sub('#', '', name).strip()
    # we allow / in page names so we want to strip each bit between slashes
    name = '/'.join([part.strip()
                     for part in name.split('/') if slugify(part)])
    return name


SLUGIFY_KEEP = r"\-\.,'\"/!@$%&*()"
def slugify(value, keep=SLUGIFY_KEEP):
    """
    Normalizes page name for db lookup

    Args:
        value: String or unicode object to normalize.
        keep: Special non-word and non-space characters that should not get
        stripped out and contribute to a slug's uniqueness. Defaults to
        characters important to meaning.
    Returns:
        Lowercase string with special characters removed.
    """
    value = url_to_name(value)

    # normalize unicode
    import unicodedata
    value = unicodedata.normalize('NFKD', unicode(value))

    # remove non-{word,space,keep} characters
    misc_characters = re.compile(('[^\w\s%s]' % keep), re.UNICODE)
    value = re.sub(misc_characters, '', value)
    value = value.strip()
    value = re.sub('[_\s]+', ' ', value)

    return value.lower()
slugify = stringfilter(slugify)


def name_to_url(value):
    """
    Converts page name to its canonical URL path
    """
    # spaces to underscore
    # This is performance-critical, sad name_to_url can be called
    # thousands of times on some results (e.g. map points' urls).
    # Still faster than re.sub.
    value = value.strip().replace(' ', '_').replace('\t', '_'
        ).replace('\r', '_').replace('\n', '_')
    # url-encode
    value = quote(value.encode('utf-8'), safe='/#')
    return mark_safe(value)
name_to_url = stringfilter(name_to_url)


def url_to_name(value):
    """
    Converts URL to the intended page name
    """
    # decode URL-encoded chars
    value = unquote_plus(value.encode('utf-8')).decode('utf-8')
    return re.sub('_', ' ', value).strip()
url_to_name = stringfilter(url_to_name)


def page_url(pagename, region):
    """
    Faster than reverse() for repeated page links.
    TODO: put this somewhere else.
    """
    slug = name_to_url(pagename)
    # Use page_base_path() to avoid reverse() overhead.
    return urljoin(page_base_path(region), slug)


# For registration calls
import signals
import feeds
