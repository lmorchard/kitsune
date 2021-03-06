from datetime import datetime, timedelta

from django.conf import settings
from django.contrib.auth.models import User
from django.contrib.sites.models import Site
from django.core import mail

import mock
from nose import SkipTest
from nose.tools import eq_
from pyquery import PyQuery as pq
from taggit.models import Tag

from questions.tests import tags_eq
from sumo.urlresolvers import reverse
from sumo.helpers import urlparams
from sumo.tests import post, get, attrs_eq
from users.tests import user, add_permission
from wiki.cron import calculate_related_documents
from wiki.events import (EditDocumentEvent, ReviewableRevisionInLocaleEvent,
                         ApproveRevisionInLocaleEvent)
from wiki.models import Document, Revision, HelpfulVote, SIGNIFICANCES
from wiki.tasks import send_reviewed_notification
from wiki.tests import TestCaseBase, document, revision, new_document_data


READY_FOR_REVIEW_EMAIL_CONTENT = """


admin submitted a new revision to the document
%s.

To review this revision, click the following
link, or paste it into your browser's location bar:

https://testserver/en-US/kb/%s/review/%s

--
Unsubscribe from these emails:
https://testserver/en-US/unsubscribe/%s?s=%s"""

DOCUMENT_EDITED_EMAIL_CONTENT = """


admin created a new revision to the document
%s.

To view this document's history, click the following
link, or paste it into your browser's location bar:

https://testserver/en-US/kb/%s/history

--
Unsubscribe from these emails:
https://testserver/en-US/unsubscribe/%s?s=%s"""

APPROVED_EMAIL_CONTENT = """

A new revision has been approved for the document
%s.

To view the updated document, click the following
link, or paste it into your browser's location bar:

https://testserver/en-US/kb/%s

--
Unsubscribe from these emails:
https://testserver/en-US/unsubscribe/%s?s=%s"""


class DocumentTests(TestCaseBase):
    """Tests for the Document template"""
    fixtures = ['users.json']

    def test_document_view(self):
        """Load the document view page and verify the title and content."""
        r = revision(save=True, content='Some text.', is_approved=True)
        response = self.client.get(r.document.get_absolute_url())
        eq_(200, response.status_code)
        doc = pq(response.content)
        eq_(r.document.title, doc('#main h1.title').text())
        eq_(pq(r.document.html)('div').text(), doc('#doc-content div').text())
        # There's a canonical URL in the <head>.
        eq_(r.document.get_absolute_url(),
            doc('link[rel=canonical]').attr('href'))

    def test_english_document_no_approved_content(self):
        """Load an English document with no approved content."""
        r = revision(save=True, content='Some text.', is_approved=False)
        response = self.client.get(r.document.get_absolute_url())
        eq_(200, response.status_code)
        doc = pq(response.content)
        eq_(r.document.title, doc('#main h1.title').text())
        eq_("This article doesn't have approved content yet.",
            doc('#doc-content').text())

    def test_translation_document_no_approved_content(self):
        """Load a non-English document with no approved content, with a parent
        with no approved content either."""
        r = revision(save=True, content='Some text.', is_approved=False)
        d2 = document(parent=r.document, locale='fr', slug='french', save=True)
        revision(document=d2, save=True, content='Moartext', is_approved=False)
        response = self.client.get(d2.get_absolute_url())
        eq_(200, response.status_code)
        doc = pq(response.content)
        eq_(d2.title, doc('#main h1.title').text())
        # Avoid depending on localization, assert just that there is only text
        # d.html would definitely have a <p> in it, at least.
        eq_(doc('#doc-content').html().strip(), doc('#doc-content').text())

    def test_document_fallback_with_translation(self):
        """The document template falls back to English if translation exists
        but it has no approved revisions."""
        r = revision(save=True, content='Test', is_approved=True)
        d2 = document(parent=r.document, locale='fr', slug='french', save=True)
        revision(document=d2, is_approved=False, save=True)
        url = reverse('wiki.document', args=[d2.slug], locale='fr')
        response = self.client.get(url)
        doc = pq(response.content)
        eq_(d2.title, doc('#main h1.title').text())

        # Fallback message is shown.
        eq_(1, len(doc('#doc-pending-fallback')))
        # Removing this as it shows up in text(), and we don't want to depend
        # on its localization.
        doc('#doc-pending-fallback').remove()
        # Included content is English.
        eq_(pq(r.document.html).text(), doc('#doc-content').text())

    def test_document_fallback_with_translation_english_slug(self):
        """The document template falls back to English if translation exists
        but it has no approved revisions, while visiting the English slug."""
        r = revision(save=True, content='Test', is_approved=True)
        d2 = document(parent=r.document, locale='fr', slug='french', save=True)
        revision(document=d2, is_approved=False, save=True)
        url = reverse('wiki.document', args=[r.document.slug], locale='fr')
        response = self.client.get(url, follow=True)
        eq_('http://testserver/fr/kb/french', response.redirect_chain[0][0])
        doc = pq(response.content)
        # Fallback message is shown.
        eq_(1, len(doc('#doc-pending-fallback')))
        # Removing this as it shows up in text(), and we don't want to depend
        # on its localization.
        doc('#doc-pending-fallback').remove()
        # Included content is English.
        eq_(pq(r.document.html).text(), doc('#doc-content').text())

    def test_document_fallback_no_translation(self):
        """The document template falls back to English if no translation
        exists."""
        r = revision(save=True, content='Some text.', is_approved=True)
        url = reverse('wiki.document', args=[r.document.slug], locale='fr')
        response = self.client.get(url)
        doc = pq(response.content)
        eq_(r.document.title, doc('#main h1.title').text())

        # Fallback message is shown.
        eq_(1, len(doc('#doc-pending-fallback')))
        # Removing this as it shows up in text(), and we don't want to depend
        # on its localization.
        doc('#doc-pending-fallback').remove()
        # Included content is English.
        eq_(pq(r.document.html)('div').text(), doc('#doc-content div').text())
        # Only two document tabs show (Article,Translate)
        eq_(2, len(doc('#doc-tabs li')))

    def test_redirect(self):
        """Make sure documents with REDIRECT directives redirect properly.

        Also check the backlink to the redirect page.

        """
        target = document(save=True)
        target_url = target.get_absolute_url()

        # Ordinarily, a document with no approved revisions cannot have HTML,
        # but we shove it in manually here as a shortcut:
        redirect = document(
                    html='<p>REDIRECT <a href="%s">Boo</a></p>' % target_url)
        redirect.save()
        redirect_url = redirect.get_absolute_url()
        response = self.client.get(redirect_url, follow=True)
        self.assertRedirects(response, urlparams(target_url,
                                                redirectlocale=redirect.locale,
                                                redirectslug=redirect.slug))
        self.assertContains(response, redirect_url + '?redirect=no')
        # There's a canonical URL in the <head>.
        doc = pq(response.content)
        eq_(target_url, doc('link[rel=canonical]').attr('href'))

    def test_redirect_from_nonexistent(self):
        """The template shouldn't crash or print a backlink if the "from" page
        doesn't exist."""
        d = document(save=True)
        response = self.client.get(urlparams(d.get_absolute_url(),
                                             redirectlocale='en-US',
                                             redirectslug='nonexistent'))
        self.assertNotContains(response, 'Redirected from ')

    def test_watch_includes_csrf(self):
        """The watch/unwatch forms should include the csrf tag."""
        self.client.login(username='jsocol', password='testpass')
        d = document(save=True)
        resp = self.client.get(d.get_absolute_url())
        doc = pq(resp.content)
        assert doc('#doc-watch input[type=hidden]')

    def test_non_localizable_translate_disabled(self):
        """Non localizable document doesn't show tab for 'Localize'."""
        self.client.login(username='jsocol', password='testpass')
        d = document(is_localizable=True, save=True)
        resp = self.client.get(d.get_absolute_url())
        doc = pq(resp.content)
        assert 'Translate' in doc('#doc-tabs li').text()

        # Make it non-localizable
        d.is_localizable = False
        d.save()
        resp = self.client.get(d.get_absolute_url())
        doc = pq(resp.content)
        assert 'Localize' not in doc('#doc-tabs li').text()

    def test_topics_enUS(self):
        """Make sure an en-US document shows the right topics."""
        d = document(is_localizable=True, save=True)
        d.tags.add('tag1', 'tag2')
        r = self.client.get(d.get_absolute_url())
        doc = pq(r.content)
        eq_('tag1 tag2', doc('li.topic a').text())

    def test_topics_es(self):
        """Make sure an es document shows the right topics (inherited)."""
        d_enUS = document(is_localizable=True, save=True)
        d_enUS.tags.add('tag1')
        d_es = document(parent=d_enUS, locale='es', save=True)
        r = self.client.get(d_es.get_absolute_url())
        doc = pq(r.content)
        eq_('tag1', doc('li.topic a').text())


class RevisionTests(TestCaseBase):
    """Tests for the Revision template"""
    fixtures = ['users.json']

    def test_revision_view(self):
        """Load the revision view page and verify the title and content."""
        d = _create_document()
        r = d.current_revision
        r.created = datetime(2011, 1, 1)
        r.reviewed = datetime(2011, 1, 2)
        r.save()
        url = reverse('wiki.revision', args=[d.slug, r.id])
        response = self.client.get(url)
        eq_(200, response.status_code)
        doc = pq(response.content)
        eq_('Revision id: %s' % r.id,
            doc('#wiki-doc div.revision-info li:first').text())
        eq_(d.title, doc('#wiki-doc h1.title').text())
        eq_(pq(r.content_parsed)('div').text(),
            doc('#doc-content div').text())
        eq_('Created:Jan 1, 2011 12:00:00 AM',
            doc('#wiki-doc div.revision-info li')[1].text_content().strip())
        eq_('Reviewed:Jan 2, 2011 12:00:00 AM',
            doc('#wiki-doc div.revision-info li')[4].text_content().strip())
        # is reviewed?
        eq_('Yes', doc('.revision-info li').eq(3).find('span').text())
        # is current revision?
        eq_('Yes', doc('.revision-info li').eq(7).find('span').text())


class NewDocumentTests(TestCaseBase):
    """Tests for the New Document template"""
    fixtures = ['users.json']

    def test_new_document_GET_with_perm(self):
        """HTTP GET to new document URL renders the form."""
        self.client.login(username='admin', password='testpass')
        response = self.client.get(reverse('wiki.new_document'))
        eq_(200, response.status_code)
        doc = pq(response.content)
        eq_(1, len(doc('#document-form input[name="title"]')))

    def test_new_document_form_defaults(self):
        """The new document form should have all all 'Relevant to' options
        checked by default."""
        self.client.login(username='admin', password='testpass')
        response = self.client.get(reverse('wiki.new_document'))
        doc = pq(response.content)
        eq_(9, len(doc('input[checked=checked]')))
        eq_(None, doc('input[name="tags"]').attr('required'))

    @mock.patch.object(ReviewableRevisionInLocaleEvent, 'fire')
    @mock.patch.object(Site.objects, 'get_current')
    def test_new_document_POST(self, get_current, ready_fire):
        """HTTP POST to new document URL creates the document."""
        get_current.return_value.domain = 'testserver'

        self.client.login(username='admin', password='testpass')
        tags = ['tag1', 'tag2']
        data = new_document_data(tags)
        response = self.client.post(reverse('wiki.new_document'), data,
                                    follow=True)
        d = Document.objects.get(title=data['title'])
        eq_([('http://testserver/en-US/kb/%s/history' % d.slug, 302)],
            response.redirect_chain)
        eq_(settings.WIKI_DEFAULT_LANGUAGE, d.locale)
        eq_(data['category'], d.category)
        eq_(tags, sorted(t.name for t in d.tags.all()))
        eq_(data['firefox_versions'],
            list(d.firefox_versions.values_list('item_id', flat=True)))
        eq_(data['operating_systems'],
            list(d.operating_systems.values_list('item_id', flat=True)))
        r = d.revisions.all()[0]
        eq_(data['keywords'], r.keywords)
        eq_(data['summary'], r.summary)
        eq_(data['content'], r.content)
        ready_fire.assert_called()

    @mock.patch.object(ReviewableRevisionInLocaleEvent, 'fire')
    @mock.patch.object(Site.objects, 'get_current')
    def test_new_document_other_locale(self, get_current, ready_fire):
        """Make sure we can create a document in a non-default locale."""
        # You shouldn't be able to make a new doc in a non-default locale
        # without marking it as non-localizable. Unskip this when the non-
        # localizable bool is implemented.
        raise SkipTest

        get_current.return_value.domain = 'testserver'

        self.client.login(username='admin', password='testpass')
        data = new_document_data(['tag1', 'tag2'])
        locale = 'es'
        self.client.post(reverse('wiki.new_document', locale=locale),
                                    data, follow=True)
        d = Document.objects.get(title=data['title'])
        eq_(locale, d.locale)
        ready_fire.assert_called()

    def test_new_document_POST_empty_title(self):
        """Trigger required field validation for title."""
        self.client.login(username='admin', password='testpass')
        data = new_document_data(['tag1', 'tag2'])
        data['title'] = ''
        response = self.client.post(reverse('wiki.new_document'), data,
                                    follow=True)
        doc = pq(response.content)
        ul = doc('#document-form > ul.errorlist')
        eq_(1, len(ul))
        eq_('Please provide a title.', ul('li').text())

    def test_new_document_POST_empty_content(self):
        """Trigger required field validation for content."""
        self.client.login(username='admin', password='testpass')
        data = new_document_data(['tag1', 'tag2'])
        data['content'] = ''
        response = self.client.post(reverse('wiki.new_document'), data,
                                    follow=True)
        doc = pq(response.content)
        ul = doc('#document-form > ul.errorlist')
        eq_(1, len(ul))
        eq_('Please provide content.', ul('li').text())

    def test_new_document_POST_invalid_category(self):
        """Try to create a new document with an invalid category value."""
        self.client.login(username='admin', password='testpass')
        data = new_document_data(['tag1', 'tag2'])
        data['category'] = 963
        response = self.client.post(reverse('wiki.new_document'), data,
                                    follow=True)
        doc = pq(response.content)
        ul = doc('#document-form > ul.errorlist')
        eq_(1, len(ul))
        assert ('Select a valid choice. 963 is not one of the available '
                'choices.' in ul('li').text())

    def test_new_document_missing_category(self):
        """Test the DocumentForm's category validation.

        Submit the form without a category set, and it should complain, even
        though it's not a strictly required field (because it cannot be set for
        translations).

        """
        self.client.login(username='admin', password='testpass')
        data = new_document_data()
        del data['category']
        response = self.client.post(reverse('wiki.new_document'), data,
                                    follow=True)
        self.assertContains(response, 'Please choose a category.')

    def test_new_document_POST_invalid_ff_version(self):
        """Try to create a new document with an invalid firefox version."""
        self.client.login(username='admin', password='testpass')
        data = new_document_data(['tag1', 'tag2'])
        data['firefox_versions'] = [1337]
        response = self.client.post(reverse('wiki.new_document'), data,
                                    follow=True)
        doc = pq(response.content)
        ul = doc('#document-form > ul.errorlist')
        eq_(1, len(ul))
        eq_('Select a valid choice. 1337 is not one of the available choices.',
            ul('li').text())

    def test_slug_collision_validation(self):
        """Trying to create document with existing locale/slug should
        show validation error."""
        d = _create_document()
        self.client.login(username='admin', password='testpass')
        data = new_document_data()
        data['slug'] = d.slug
        response = self.client.post(reverse('wiki.new_document'), data)
        eq_(200, response.status_code)
        doc = pq(response.content)
        ul = doc('#document-form > ul.errorlist')
        eq_(1, len(ul))
        eq_('Document with this Slug and Locale already exists.',
            ul('li').text())

    def test_title_collision_validation(self):
        """Trying to create document with existing locale/slug should
        show validation error."""
        d = _create_document()
        self.client.login(username='admin', password='testpass')
        data = new_document_data()
        data['title'] = d.title
        response = self.client.post(reverse('wiki.new_document'), data)
        eq_(200, response.status_code)
        doc = pq(response.content)
        ul = doc('#document-form > ul.errorlist')
        eq_(1, len(ul))
        eq_('Document with this Title and Locale already exists.',
            ul('li').text())

    @mock.patch.object(Site.objects, 'get_current')
    def test_slug_3_chars(self, get_current):
        """Make sure we can create a slug with only 3 characters."""
        get_current.return_value.domain = 'testserver'
        self.client.login(username='admin', password='testpass')
        data = new_document_data()
        data['slug'] = 'ask'
        response = self.client.post(reverse('wiki.new_document'), data)
        eq_(302, response.status_code)
        eq_('ask', Document.objects.all()[0].slug)


class NewRevisionTests(TestCaseBase):
    """Tests for the New Revision template"""
    fixtures = ['users.json']

    def setUp(self):
        super(NewRevisionTests, self).setUp()
        self.d = _create_document()
        self.client.login(username='admin', password='testpass')

    def test_new_revision_GET_logged_out(self):
        """Creating a revision without being logged in redirects to login page.
        """
        self.client.logout()
        response = self.client.get(reverse('wiki.edit_document',
                                           args=[self.d.slug]))
        eq_(302, response.status_code)

    def test_new_revision_GET_with_perm(self):
        """HTTP GET to new revision URL renders the form."""
        response = self.client.get(reverse('wiki.edit_document',
                                           args=[self.d.slug]))
        eq_(200, response.status_code)
        doc = pq(response.content)
        eq_(1, len(doc('#revision-form textarea[name="content"]')))
        assert 'value' not in doc('#id_comment')[0].attrib

    def test_new_revision_GET_based_on(self):
        """HTTP GET to new revision URL based on another revision.

        This case should render the form with the fields pre-populated
        with the based-on revision info.

        """
        r = Revision(document=self.d, keywords='ky1, kw2',
                     summary='the summary',
                     content='<div>The content here</div>', creator_id=118577)
        r.save()
        response = self.client.get(reverse('wiki.new_revision_based_on',
                                           args=[self.d.slug, r.id]))
        eq_(200, response.status_code)
        doc = pq(response.content)
        eq_(doc('#id_keywords')[0].value, r.keywords)
        eq_(doc('#id_summary')[0].value, r.summary)
        eq_(doc('#id_content')[0].value, r.content)

    @mock.patch.object(Site.objects, 'get_current')
    @mock.patch.object(settings._wrapped, 'TIDINGS_CONFIRM_ANONYMOUS_WATCHES', False)
    def test_new_revision_POST_document_with_current(self, get_current):
        """HTTP POST to new revision URL creates the revision on a document.

        The document in this case already has a current_revision, therefore
        the document document fields are not editable.

        Also assert that the edited and reviewable notifications go out.

        """
        get_current.return_value.domain = 'testserver'

        # Sign up for notifications:
        edit_watch = EditDocumentEvent.notify('sam@example.com', self.d)
        edit_watch.activate().save()
        reviewable_watch = ReviewableRevisionInLocaleEvent.notify(
            'joe@example.com', locale='en-US')
        reviewable_watch.activate().save()

        # Edit a document:
        response = self.client.post(
            reverse('wiki.edit_document', args=[self.d.slug]),
            {'summary': 'A brief summary', 'content': 'The article content',
             'keywords': 'keyword1 keyword2',
             'based_on': self.d.current_revision.id, 'form': 'rev'})
        eq_(302, response.status_code)
        eq_(2, self.d.revisions.count())
        new_rev = self.d.revisions.order_by('-id')[0]
        eq_(self.d.current_revision, new_rev.based_on)

        # Assert notifications fired and have the expected content:
        attrs_eq(mail.outbox[0],
                 subject=u'%s is ready for review (%s)' % (self.d.title,
                                                           new_rev.creator),
                 body=READY_FOR_REVIEW_EMAIL_CONTENT %
                    (self.d.title, self.d.slug, new_rev.id,
                     reviewable_watch.pk, reviewable_watch.secret),
                 to=['joe@example.com'])
        attrs_eq(mail.outbox[1],
                 subject=u'%s was edited by %s' % (self.d.title,
                                                   new_rev.creator),
                 body=DOCUMENT_EDITED_EMAIL_CONTENT %
                    (self.d.title, self.d.slug, edit_watch.pk,
                     edit_watch.secret),
                 to=['sam@example.com'])

    @mock.patch.object(ReviewableRevisionInLocaleEvent, 'fire')
    @mock.patch.object(EditDocumentEvent, 'fire')
    @mock.patch.object(Site.objects, 'get_current')
    def test_new_revision_POST_document_without_current(
            self, get_current, edited_fire, ready_fire):
        """HTTP POST to new revision URL creates the revision on a document.

        The document in this case doesn't have a current_revision, therefore
        the document fields are open for editing.

        """
        get_current.return_value.domain = 'testserver'

        self.d.current_revision = None
        self.d.save()
        tags = ['tag1', 'tag2', 'tag3']
        data = new_document_data(tags)
        data['form'] = 'rev'
        response = self.client.post(reverse('wiki.edit_document',
                                    args=[self.d.slug]), data)
        eq_(302, response.status_code)
        eq_(2, self.d.revisions.count())

        new_rev = self.d.revisions.order_by('-id')[0]
        # There are no approved revisions, so it's based_on nothing:
        eq_(None, new_rev.based_on)
        edited_fire.assert_called()
        ready_fire.assert_called()

    def test_new_revision_POST_removes_old_tags(self):
        """Changing the tags on a document removes the old tags from
        that document."""
        self.d.current_revision = None
        self.d.save()
        tags = ['tag1', 'tag2', 'tag3']
        self.d.tags.add(*tags)
        tags_eq(self.d, tags)
        tags = ['tag1', 'tag4']
        data = new_document_data(tags)
        data['form'] = 'doc'
        self.client.post(reverse('wiki.edit_document', args=[self.d.slug]),
                         data)
        tags_eq(self.d, tags)

    @mock.patch.object(Site.objects, 'get_current')
    def test_new_form_maintains_based_on_rev(self, get_current):
        """Revision.based_on should be the rev that was current when the Edit
        button was clicked, even if other revisions happen while the user is
        editing."""
        get_current.return_value.domain = 'testserver'
        _test_form_maintains_based_on_rev(
            self.client, self.d, 'wiki.edit_document',
            {'summary': 'Windy', 'content': 'gerbils', 'form': 'rev'},
            locale=None)

    def test_new_revision_warning(self):
        """When editing based on current revision, we should show a warning if
        there are newer unapproved revisions."""
        # Create a new revision that is at least 1 second newer than current
        created = datetime.now() + timedelta(seconds=1)
        r = revision(document=self.d, created=created, save=True)

        # Verify there is a warning box
        response = self.client.get(reverse('wiki.edit_document',
                                           args=[self.d.slug]))
        assert len(pq(response.content)('div.warning-box'))

        # Verify there is no warning box if editing the latest unreviewed
        response = self.client.get(reverse('wiki.new_revision_based_on',
                                           args=[self.d.slug, r.id]))
        assert not len(pq(response.content)('div.warning-box'))

        # Create a newer unreviewed revision and now warning shows
        created =created + timedelta(seconds=1)
        revision(document=self.d, created=created, save=True)
        response = self.client.get(reverse('wiki.new_revision_based_on',
                                           args=[self.d.slug, r.id]))
        assert len(pq(response.content)('div.warning-box'))


class DocumentEditTests(TestCaseBase):
    """Test the editing of document level fields."""
    fixtures = ['users.json']

    def setUp(self):
        super(DocumentEditTests, self).setUp()
        self.d = _create_document()
        self.client.login(username='admin', password='testpass')

    def test_can_save_document_with_translations(self):
        """Make sure we can save a document with translations."""
        # Create a translation
        _create_document(title='Document Prueba', parent=self.d,
                             locale='es')
        # Make sure is_localizable hidden field is rendered
        response = get(self.client, 'wiki.edit_document', args=[self.d.slug])
        eq_(200, response.status_code)
        doc = pq(response.content)
        is_localizable = doc('input[name="is_localizable"]')
        eq_(1, len(is_localizable))
        eq_('True', is_localizable[0].attrib['value'])
        # And make sure we can update the document
        data = new_document_data()
        new_title = 'A brand new title'
        data.update(title=new_title)
        data.update(form='doc')
        data.update(is_localizable='True')
        response = post(self.client, 'wiki.edit_document', data,
                        args=[self.d.slug])
        eq_(200, response.status_code)
        doc = Document.uncached.get(pk=self.d.pk)
        eq_(new_title, doc.title)

    def test_change_slug_case(self):
        """Changing the case of some letters in the slug should work."""
        data = new_document_data()
        new_slug = 'Test-Document'
        data.update(slug=new_slug)
        data.update(form='doc')
        response = post(self.client, 'wiki.edit_document', data,
                        args=[self.d.slug])
        eq_(200, response.status_code)
        doc = Document.uncached.get(pk=self.d.pk)
        eq_(new_slug, doc.slug)

    def test_change_title_case(self):
        """Changing the case of some letters in the title should work."""
        data = new_document_data()
        new_title = 'TeST DoCuMent'
        data.update(title=new_title)
        data.update(form='doc')
        response = post(self.client, 'wiki.edit_document', data,
                        args=[self.d.slug])
        eq_(200, response.status_code)
        doc = Document.uncached.get(pk=self.d.pk)
        eq_(new_title, doc.title)

    def test_archive_permission_off(self):
        """Shouldn't be able to change is_archive bit without permission."""
        u = user(save=True)
        add_permission(u, Document, 'change_document')
        self.client.login(username=u.username, password='testpass')
        data = new_document_data()
        # Try to set is_archived, even though we shouldn't have permission to:
        data.update(form='doc', is_archived='on')
        response = post(self.client, 'wiki.edit_document', data,
                        args=[self.d.slug])
        eq_(200, response.status_code)
        doc = Document.uncached.get(pk=self.d.pk)
        assert not doc.is_archived

    # TODO: Factor with test_archive_permission_off.
    def test_archive_permission_on(self):
        """Shouldn't be able to change is_archive bit without permission."""
        u = user(save=True)
        add_permission(u, Document, 'change_document')
        add_permission(u, Document, 'archive_document')
        self.client.login(username=u.username, password='testpass')
        data = new_document_data()
        data.update(form='doc', is_archived='on')
        response = post(self.client, 'wiki.edit_document', data,
                        args=[self.d.slug])
        eq_(200, response.status_code)
        doc = Document.uncached.get(pk=self.d.pk)
        assert doc.is_archived


class DocumentListTests(TestCaseBase):
    """Tests for the All and Category template"""
    fixtures = ['users.json']

    def setUp(self):
        super(DocumentListTests, self).setUp()
        self.locale = settings.WIKI_DEFAULT_LANGUAGE
        self.doc = _create_document(locale=self.locale)
        _create_document(locale=self.locale, title='Another one')

        # Create a document in different locale to make sure it doesn't show
        _create_document(parent=self.doc, locale='es')

    def test_category_list(self):
        """Verify the category documents list view."""
        response = self.client.get(reverse('wiki.category',
                                   args=[self.doc.category]))
        doc = pq(response.content)
        cat = self.doc.category
        eq_(Document.objects.filter(category=cat, locale=self.locale).count(),
            len(doc('#document-list ul.documents li')))

    def test_all_list(self):
        """Verify the all documents list view."""
        response = self.client.get(reverse('wiki.all_documents'))
        doc = pq(response.content)
        eq_(Document.objects.filter(locale=self.locale).count(),
            len(doc('#document-list ul.documents li')))

    def test_tag_list(self):
        """Verify the tagged documents list view."""
        tag = Tag(name='Test Tag', slug='test-tag')
        tag.save()
        self.doc.tags.add(tag)
        response = self.client.get(reverse('wiki.tag',
                                   args=[tag.slug]))
        eq_(200, response.status_code)
        doc = pq(response.content)
        eq_(1, len(doc('#document-list ul.documents li')))


class DocumentRevisionsTests(TestCaseBase):
    """Tests for the Document Revisions template"""
    fixtures = ['users.json']

    def test_document_revisions_list(self):
        """Verify the document revisions list view."""
        d = _create_document()
        user_ = User.objects.get(pk=118533)
        r1 = revision(summary="a tweak", content='lorem ipsum dolor',
                      keywords='kw1 kw2', document=d, creator=user_)
        r1.save()
        r2 = revision(summary="another tweak", content='lorem dimsum dolor',
                      keywords='kw1 kw2', document=d, creator=user_)
        r2.save()
        response = self.client.get(reverse('wiki.document_revisions',
                                   args=[d.slug]))
        eq_(200, response.status_code)
        doc = pq(response.content)
        eq_(4, len(doc('#revision-list li')))
        # Verify there is no Review link
        eq_(0, len(doc('#revision-list div.status a')))
        eq_('Unreviewed', doc('#revision-list div.status:first').text())

        # Log in as user with permission to review
        self.client.login(username='admin', password='testpass')
        response = self.client.get(reverse('wiki.document_revisions',
                                   args=[d.slug]))
        eq_(200, response.status_code)
        doc = pq(response.content)
        # Verify there are Review links now
        eq_(2, len(doc('#revision-list div.status a')))
        eq_('Review', doc('#revision-list div.status:first').text())
        # Verify edit revision link
        eq_('/en-US/kb/test-document/edit/{r}'.format(r=r2.id),
            doc('#revision-list div.edit a')[0].attrib['href'])


class ReviewRevisionTests(TestCaseBase):
    """Tests for Review Revisions and Translations"""
    fixtures = ['users.json']

    def setUp(self):
        super(ReviewRevisionTests, self).setUp()
        self.document = _create_document()
        user_ = User.objects.get(pk=118533)
        self.revision = Revision(summary="lipsum",
                                 content='<div>Lorem {for mac}Ipsum{/for} '
                                         'Dolor</div>',
                                 keywords='kw1 kw2', document=self.document,
                                 creator=user_)
        self.revision.save()

        self.client.login(username='admin', password='testpass')

    def test_fancy_renderer(self):
        """Make sure it renders the whizzy new wiki syntax."""
        # The right branch of the template renders only when there's no current
        # revision.
        self.document.current_revision = None
        self.document.save()

        response = get(self.client, 'wiki.review_revision',
                       args=[self.document.slug, self.revision.id])

        # Does the {for} syntax seem to have rendered?
        assert pq(response.content)('span[class=for]')

    @mock.patch.object(send_reviewed_notification, 'delay')
    @mock.patch.object(Site.objects, 'get_current')
    @mock.patch.object(settings._wrapped, 'TIDINGS_CONFIRM_ANONYMOUS_WATCHES', False)
    def test_approve_revision(self, get_current, reviewed_delay):
        """Verify revision approval with proper notifications."""
        get_current.return_value.domain = 'testserver'

        # Subscribe to approvals:
        watch = ApproveRevisionInLocaleEvent.notify('joe@example.com',
                                                    locale='en-US')
        watch.activate().save()

        # Approve something:
        significance = SIGNIFICANCES[0][0]
        response = post(self.client, 'wiki.review_revision',
                        {'approve': 'Approve Revision',
                         'significance': significance},
                        args=[self.document.slug, self.revision.id])

        eq_(200, response.status_code)
        r = Revision.uncached.get(pk=self.revision.id)
        eq_(significance, r.significance)
        assert r.reviewed
        assert r.is_approved

        # The "reviewed" mail should be sent to the creator, and the "approved"
        # mail should be sent to any subscribers:
        reviewed_delay.assert_called_with(r, r.document, '')
        attrs_eq(mail.outbox[0],
                 subject='%s (%s) has a new approved revision' %
                     (self.document.title, self.document.locale),
                 body=APPROVED_EMAIL_CONTENT %
                    (self.document.title, self.document.slug, watch.pk,
                     watch.secret),
                 to=['joe@example.com'])

    @mock.patch.object(send_reviewed_notification, 'delay')
    @mock.patch.object(Site.objects, 'get_current')
    def test_reject_revision(self, get_current, delay):
        """Verify revision rejection."""
        get_current.return_value.domain = 'testserver'

        comment = 'no good'
        response = post(self.client, 'wiki.review_revision',
                        {'reject': 'Reject Revision',
                         'comment': comment},
                        args=[self.document.slug, self.revision.id])
        eq_(200, response.status_code)
        r = Revision.uncached.get(pk=self.revision.id)
        assert r.reviewed
        assert not r.is_approved
        delay.assert_called_with(r, r.document, comment)

    def test_review_without_permission(self):
        """Make sure unauthorized users can't review revisions."""
        self.client.login(username='rrosario', password='testpass')
        response = post(self.client, 'wiki.review_revision',
                        {'reject': 'Reject Revision'},
                        args=[self.document.slug, self.revision.id])
        eq_(403, response.status_code)

    def test_review_logged_out(self):
        """Make sure logged out users can't review revisions."""
        self.client.logout()
        response = post(self.client, 'wiki.review_revision',
                        {'reject': 'Reject Revision'},
                        args=[self.document.slug, self.revision.id])
        redirect = response.redirect_chain[0]
        eq_(302, redirect[1])
        eq_('http://testserver/%s%s?next=/en-US/kb/test-document/review/%s' %
            (settings.LANGUAGE_CODE, settings.LOGIN_URL,
                 str(self.revision.id)),
            redirect[0])

    @mock.patch.object(Site.objects, 'get_current')
    def test_review_translation(self, get_current):
        """Make sure it works for localizations as well."""
        get_current.return_value.domain = 'testserver'
        doc = self.document
        user_ = User.objects.get(pk=118533)

        # Create the translated document based on the current revision
        doc_es = _create_document(locale='es', parent=doc)
        rev_es1 = doc_es.current_revision
        rev_es1.based_on = doc.current_revision
        rev_es1.save()

        # Add a new revision to the parent and set it as the current one
        rev = revision(summary="another tweak", content='lorem dimsum dolor',
                       significance=SIGNIFICANCES[0][0], keywords='kw1 kw2',
                       document=doc, creator=user_, is_approved=True,
                       based_on=self.revision)
        rev.save()

        # Create a new translation based on the new current revision
        rev_es2 = Revision(summary="lipsum",
                          content='<div>Lorem {for mac}Ipsum{/for} '
                                  'Dolor</div>',
                          keywords='kw1 kw2', document=doc_es,
                          creator=user_, based_on=doc.current_revision)
        rev_es2.save()

        # Whew, now render the review page
        self.client.login(username='admin', password='testpass')
        url = reverse('wiki.review_revision', locale='es',
                      args=[doc_es.slug, rev_es2.id])
        response = self.client.get(url, follow=True)
        eq_(200, response.status_code)
        doc = pq(response.content)
        diff_heading = doc('div.revision-diff h3').text()
        assert str(rev_es1.based_on.id) in diff_heading
        assert str(rev.id) in diff_heading

        # And finally, approve the translation
        response = self.client.post(url, {'approve': 'Approve Translation'},
                                    follow=True)
        eq_(200, response.status_code)
        d = Document.uncached.get(pk=doc_es.id)
        r = Revision.uncached.get(pk=rev_es2.id)
        eq_(d.current_revision, r)
        assert r.reviewed
        assert r.is_approved

    def test_review_translation_of_unapproved_parent(self):
        """Translate unapproved English document a 2nd time.

        Reviewing a revision of a translation when the English document
        does not have a current revision should fall back to the latest
        English revision.

        """
        en_revision = revision(is_approved=False, save=True)

        # Create the translated document based on the current revision
        es_document = document(locale='es', parent=en_revision.document,
                               save=True)
        # Create first revision
        revision(document=es_document, is_approved=True, save=True)
        es_revision = revision(document=es_document, reviewed=None,
                               is_approved=False,
                               reviewer=None, save=True)

        # Now render the review page
        self.client.login(username='admin', password='testpass')
        url = reverse('wiki.review_revision',
                      args=[es_document.slug, es_revision.id])
        response = self.client.get(url, follow=True)
        eq_(200, response.status_code)
        doc = pq(response.content)
        # There's no 'Recent English Changes' <details> section
        eq_(3, len(doc('details')))
        eq_('Approved English version:',
            doc('#content-fields h3').eq(0).text())
        rev_message = doc('#content-fields p').eq(0).text()
        assert 'by jsocol' in rev_message, ('%s does not contain "by jsocol"'
                                            % rev_message)

    def test_review_translation_of_rejected_parent(self):
        """Translate rejected English document a 2nd time.

        Reviewing a revision of a translation when the English document
        has only rejected revisions should show a message.

        """
        user_ = User.objects.get(pk=118533)
        en_revision = revision(is_approved=False, save=True, reviewer=user_,
                               reviewed=datetime.now())

        # Create the translated document based on the current revision
        es_document = document(locale='es', parent=en_revision.document,
                               save=True)
        # Create first revision
        revision(document=es_document, is_approved=True, save=True)
        es_revision = revision(document=es_document, reviewed=None,
                               is_approved=False,
                               reviewer=None, save=True)

        # Now render the review page
        self.client.login(username='admin', password='testpass')
        url = reverse('wiki.review_revision',
                      args=[es_document.slug, es_revision.id])
        response = self.client.get(url, follow=True)
        eq_(200, response.status_code)
        doc = pq(response.content)
        # There's no 'Recent English Changes' <details> section
        eq_(3, len(doc('details')))
        eq_('The English version has no approved content to show.',
            doc('details .warning-box').text())


class CompareRevisionTests(TestCaseBase):
    """Tests for Review Revisions"""
    fixtures = ['users.json']

    def setUp(self):
        super(CompareRevisionTests, self).setUp()
        self.document = _create_document()
        self.revision1 = self.document.current_revision
        user_ = User.objects.get(pk=118533)
        self.revision2 = Revision(summary="lipsum",
                                 content='<div>Lorem Ipsum Dolor</div>',
                                 keywords='kw1 kw2',
                                 document=self.document, creator=user_)
        self.revision2.save()

        self.client.login(username='admin', password='testpass')

    def test_compare_revisions(self):
        """Compare two revisions"""
        url = reverse('wiki.compare_revisions', args=[self.document.slug])
        query = {'from': self.revision1.id, 'to': self.revision2.id}
        url = urlparams(url, **query)
        response = self.client.get(url)
        eq_(200, response.status_code)
        doc = pq(response.content)
        eq_('Dolor',  doc('div.revision-diff span.diff_add').text())

    def test_compare_revisions_invalid_to_int(self):
        """Provide invalid 'to' int for revision ids."""
        url = reverse('wiki.compare_revisions', args=[self.document.slug])
        query = {'from': '', 'to': 'invalid'}
        url = urlparams(url, **query)
        response = self.client.get(url)
        eq_(404, response.status_code)

    def test_compare_revisions_invalid_from_int(self):
        """Provide invalid 'from' int for revision ids."""
        url = reverse('wiki.compare_revisions', args=[self.document.slug])
        query = {'from': 'invalid', 'to': ''}
        url = urlparams(url, **query)
        response = self.client.get(url)
        eq_(404, response.status_code)

    def test_compare_revisions_missing_query_param(self):
        """Try to compare two revisions, with a missing query string param."""
        url = reverse('wiki.compare_revisions', args=[self.document.slug])
        query = {'from': self.revision1.id}
        url = urlparams(url, **query)
        response = self.client.get(url)
        eq_(404, response.status_code)

        url = reverse('wiki.compare_revisions', args=[self.document.slug])
        query = {'to': self.revision1.id}
        url = urlparams(url, **query)
        response = self.client.get(url)
        eq_(404, response.status_code)


class TranslateTests(TestCaseBase):
    """Tests for the Translate page"""
    fixtures = ['users.json']

    def setUp(self):
        super(TranslateTests, self).setUp()
        self.d = _create_document()
        self.client.login(username='admin', password='testpass')

    def test_translate_GET_logged_out(self):
        """Try to create a translation while logged out."""
        self.client.logout()
        url = reverse('wiki.translate', locale='es', args=[self.d.slug])
        response = self.client.get(url)
        eq_(302, response.status_code)

    def test_translate_GET_with_perm(self):
        """HTTP GET to translate URL renders the form."""
        url = reverse('wiki.translate', locale='es', args=[self.d.slug])
        response = self.client.get(url)
        eq_(200, response.status_code)
        doc = pq(response.content)
        eq_(1, len(doc('form textarea[name="content"]')))
        assert 'value' not in doc('#id_comment')[0].attrib

    def test_translate_disallow(self):
        """HTTP GET to translate URL returns 400 when not localizable."""
        self.d.is_localizable = False
        self.d.save()
        url = reverse('wiki.translate', locale='es', args=[self.d.slug])
        response = self.client.get(url)
        eq_(400, response.status_code)

    def test_invalid_document_form(self):
        """Make sure we handle invalid document form without a 500."""
        url = reverse('wiki.translate', locale='es', args=[self.d.slug])
        data = _translation_data()
        data['slug'] = ''  # Invalid slug
        response = self.client.post(url, data)
        eq_(200, response.status_code)

    def test_invalid_revision_form(self):
        """When creating a new translation, an invalid revision form shouldn't
        result in a new Document being created."""
        url = reverse('wiki.translate', locale='es', args=[self.d.slug])
        data = _translation_data()
        data['content'] = ''  # Content is required
        response = self.client.post(url, data)
        eq_(200, response.status_code)
        eq_(0, self.d.translations.count())

    @mock.patch.object(ReviewableRevisionInLocaleEvent, 'fire')
    @mock.patch.object(EditDocumentEvent, 'fire')
    @mock.patch.object(Site.objects, 'get_current')
    def test_first_translation_to_locale(self, get_current, edited_fire,
                                         ready_fire):
        """Create the first translation of a doc to new locale."""
        get_current.return_value.domain = 'testserver'

        url = reverse('wiki.translate', locale='es', args=[self.d.slug])
        data = _translation_data()
        response = self.client.post(url, data)
        eq_(302, response.status_code)
        new_doc = Document.objects.get(slug=data['slug'])
        eq_('es', new_doc.locale)
        eq_(data['title'], new_doc.title)
        eq_(self.d, new_doc.parent)
        rev = new_doc.revisions.all()[0]
        eq_(data['keywords'], rev.keywords)
        eq_(data['summary'], rev.summary)
        eq_(data['content'], rev.content)
        edited_fire.assert_called()
        ready_fire.assert_called()

    def _create_and_approve_first_translation(self):
        """Returns the revision."""
        # First create the first one with test above
        self.test_first_translation_to_locale()
        # Approve the translation
        rev_es = Revision.objects.filter(document__locale='es')[0]
        rev_es.is_approved = True
        rev_es.save()
        return rev_es

    @mock.patch.object(ReviewableRevisionInLocaleEvent, 'fire')
    @mock.patch.object(EditDocumentEvent, 'fire')
    @mock.patch.object(Site.objects, 'get_current')
    def test_another_translation_to_locale(self, get_current, edited_fire,
                                           ready_fire):
        """Create the second translation of a doc."""
        get_current.return_value.domain = 'testserver'

        rev_es = self._create_and_approve_first_translation()

        # Create and approve a new en-US revision
        rev_enUS = Revision(summary="lipsum",
                       content='lorem ipsum dolor sit amet new',
                       significance=SIGNIFICANCES[0][0], keywords='kw1 kw2',
                       document=self.d, creator_id=118577, is_approved=True)
        rev_enUS.save()

        # Verify the form renders with correct content
        url = reverse('wiki.translate', locale='es', args=[self.d.slug])
        response = self.client.get(url)
        doc = pq(response.content)
        eq_(rev_es.content, doc('#id_content').text())
        eq_(rev_enUS.content, doc('#content-fields textarea[readonly]').text())

        # Post the translation and verify
        data = _translation_data()
        data['content'] = 'loremo ipsumo doloro sito ameto nuevo'
        response = self.client.post(url, data)
        eq_(302, response.status_code)
        eq_('http://testserver/es/kb/un-test-articulo/history',
            response['location'])
        doc = Document.objects.get(slug=data['slug'])
        rev = doc.revisions.filter(content=data['content'])[0]
        eq_(data['keywords'], rev.keywords)
        eq_(data['summary'], rev.summary)
        eq_(data['content'], rev.content)
        assert not rev.is_approved
        edited_fire.assert_called()
        ready_fire.assert_called()

    @mock.patch.object(Site.objects, 'get_current')
    def test_translate_form_maintains_based_on_rev(self, get_current):
        """Revision.based_on should be the rev that was current when the
        Translate button was clicked, even if other revisions happen while the
        user is editing."""
        get_current.return_value.domain = 'testserver'
        _test_form_maintains_based_on_rev(self.client, self.d,
                                          'wiki.translate',
                                          _translation_data(), locale='es')

    def test_translate_update_doc_only(self):
        """Submitting the document form should update document. No new
        revisions should be created."""
        rev_es = self._create_and_approve_first_translation()
        url = reverse('wiki.translate', locale='es', args=[self.d.slug])
        data = _translation_data()
        new_title = 'Un nuevo titulo'
        data['title'] = new_title
        data['form'] = 'doc'
        response = self.client.post(url, data)
        eq_(302, response.status_code)
        eq_('http://testserver/es/kb/un-test-articulo/edit?opendescription=1',
            response['location'])
        revisions = rev_es.document.revisions.all()
        eq_(1, revisions.count())  # No new revisions
        d = Document.objects.get(id=rev_es.document.id)
        eq_(new_title, d.title)  # Title is updated

    @mock.patch.object(Site.objects, 'get_current')
    def test_translate_update_rev_only(self, get_current):
        """Submitting the revision form should create a new revision.
        No document fields should be updated."""
        get_current.return_value.domain = 'testserver'
        rev_es = self._create_and_approve_first_translation()
        orig_title = rev_es.document.title
        url = reverse('wiki.translate', locale='es', args=[self.d.slug])
        data = _translation_data()
        new_title = 'Un nuevo titulo'
        data['title'] = new_title
        data['form'] = 'rev'
        response = self.client.post(url, data)
        eq_(302, response.status_code)
        eq_('http://testserver/es/kb/un-test-articulo/history',
            response['location'])
        revisions = rev_es.document.revisions.all()
        eq_(2, revisions.count())  # New revision is created
        d = Document.objects.get(id=rev_es.document.id)
        eq_(orig_title, d.title)  # Title isn't updated

    def test_translate_form_content_fallback(self):
        """If there are existing but unapproved translations, prefill
        content with latest."""
        self.test_first_translation_to_locale()
        url = reverse('wiki.translate', locale='es', args=[self.d.slug])
        response = self.client.get(url)
        doc = pq(response.content)
        document = Document.objects.filter(locale='es')[0]
        existing_rev = document.revisions.all()[0]
        eq_(existing_rev.content, doc('#id_content').text())

    def test_translate_based_on(self):
        """Test translating based on a non-current revision."""
        # Create the base revision
        base_rev = self._create_and_approve_first_translation()
        # Create a new current revision
        r = revision(document=base_rev.document, is_approved=True)
        r.save()
        d = Document.objects.get(pk=base_rev.document.id)
        eq_(r, base_rev.document.current_revision)

        url = reverse('wiki.new_revision_based_on', locale='es',
                      args=[d.slug, base_rev.id])
        response = self.client.get(url)
        eq_(200, response.status_code)
        doc = pq(response.content)
        eq_(doc('#id_content')[0].value, base_rev.content)

    def test_translate_rejected_parent(self):
        """Translate view of rejected English document shows warning."""
        user_ = User.objects.get(pk=118533)
        en_revision = revision(is_approved=False, save=True, reviewer=user_,
                               reviewed=datetime.now())

        url = reverse('wiki.translate', locale='es',
                      args=[en_revision.document.slug])
        response = self.client.get(url)
        doc = pq(response.content)
        assert doc('.warning-box').text()


def _test_form_maintains_based_on_rev(client, doc, view, post_data,
                                      locale=None):
    """Confirm that the based_on value set in the revision created by an edit
    or translate form is the current_revision of the document as of when the
    form was first loaded, even if other revisions have been approved in the
    meantime."""
    response = client.get(reverse(view, locale=locale, args=[doc.slug]))
    orig_rev = doc.current_revision
    eq_(orig_rev.id,
        int(pq(response.content)('input[name=based_on]').attr('value')))

    # While Fred is editing the above, Martha approves a new rev:
    martha_rev = revision(document=doc)
    martha_rev.is_approved = True
    martha_rev.save()

    # Then Fred saves his edit:
    post_data_copy = {'based_on': orig_rev.id}
    post_data_copy.update(post_data)  # Don't mutate arg.
    response = client.post(reverse(view, locale=locale, args=[doc.slug]),
                           data=post_data_copy)
    eq_(302, response.status_code)
    fred_rev = Revision.objects.all().order_by('-id')[0]
    eq_(orig_rev, fred_rev.based_on)


class DocumentWatchTests(TestCaseBase):
    """Tests for un/subscribing to document edit notifications."""
    fixtures = ['users.json']

    def setUp(self):
        super(DocumentWatchTests, self).setUp()
        self.document = _create_document()
        self.client.login(username='rrosario', password='testpass')

    def test_watch_GET_405(self):
        """Watch document with HTTP GET results in 405."""
        response = get(self.client, 'wiki.document_watch',
                       args=[self.document.slug])
        eq_(405, response.status_code)

    def test_unwatch_GET_405(self):
        """Unwatch document with HTTP GET results in 405."""
        response = get(self.client, 'wiki.document_unwatch',
                       args=[self.document.slug])
        eq_(405, response.status_code)

    def test_watch_unwatch(self):
        """Watch and unwatch a document."""
        user_ = User.objects.get(username='rrosario')
        # Subscribe
        response = post(self.client, 'wiki.document_watch',
                       args=[self.document.slug])
        eq_(200, response.status_code)
        assert EditDocumentEvent.is_notifying(user_, self.document), \
               'Watch was not created'
        # Unsubscribe
        response = post(self.client, 'wiki.document_unwatch',
                       args=[self.document.slug])
        eq_(200, response.status_code)
        assert not EditDocumentEvent.is_notifying(user_, self.document), \
               'Watch was not destroyed'


class LocaleWatchTests(TestCaseBase):
    """Tests for un/subscribing to a locale's ready for review emails."""
    fixtures = ['users.json']

    def setUp(self):
        super(LocaleWatchTests, self).setUp()
        self.client.login(username='rrosario', password='testpass')

    def test_watch_GET_405(self):
        """Watch document with HTTP GET results in 405."""
        response = get(self.client, 'wiki.locale_watch')
        eq_(405, response.status_code)

    def test_unwatch_GET_405(self):
        """Unwatch document with HTTP GET results in 405."""
        response = get(self.client, 'wiki.locale_unwatch')
        eq_(405, response.status_code)

    def test_watch_unwatch(self):
        """Watch and unwatch a document."""
        user_ = User.objects.get(username='rrosario')

        # Subscribe
        response = post(self.client, 'wiki.locale_watch')
        eq_(200, response.status_code)
        assert ReviewableRevisionInLocaleEvent.is_notifying(user_,
                                                            locale='en-US')

        # Unsubscribe
        response = post(self.client, 'wiki.locale_unwatch')
        eq_(200, response.status_code)
        assert not ReviewableRevisionInLocaleEvent.is_notifying(user_,
                                                                locale='en-US')


class ArticlePreviewTests(TestCaseBase):
    """Tests for preview view and template."""
    fixtures = ['users.json']

    def setUp(self):
        super(ArticlePreviewTests, self).setUp()
        self.client.login(username='rrosario', password='testpass')

    def test_preview_GET_405(self):
        """Preview with HTTP GET results in 405."""
        response = get(self.client, 'wiki.preview')
        eq_(405, response.status_code)

    def test_preview(self):
        """Preview the wiki syntax content."""
        response = post(self.client, 'wiki.preview',
                        {'content': '=Test Content='})
        eq_(200, response.status_code)
        doc = pq(response.content)
        eq_('Test Content', doc('#doc-content h1').text())

    def test_preview_locale(self):
        """Preview the wiki syntax content."""
        # Create a test document and translation.
        d = _create_document()
        _create_document(title='Prueba', parent=d, locale='es')
        # Preview content that links to it and verify link is in locale.
        url = reverse('wiki.preview', locale='es')
        response = self.client.post(url, {'content': '[[Test Document]]'})
        eq_(200, response.status_code)
        doc = pq(response.content)
        link = doc('#doc-content a')
        eq_('Prueba', link.text())
        eq_('/es/kb/prueba', link[0].attrib['href'])


class HelpfulVoteTests(TestCaseBase):
    fixtures = ['users.json']

    def setUp(self):
        super(HelpfulVoteTests, self).setUp()
        self.document = _create_document()

    def test_vote_yes(self):
        """Test voting helpful."""
        d = self.document
        user_ = User.objects.get(username='rrosario')
        self.client.login(username='rrosario', password='testpass')
        response = post(self.client, 'wiki.document_vote',
                        {'helpful': 'Yes'}, args=[self.document.slug])
        eq_(200, response.status_code)
        votes = HelpfulVote.objects.filter(document=d, creator=user_)
        eq_(1, votes.count())
        assert votes[0].helpful

    def test_vote_no(self):
        """Test voting not helpful."""
        d = self.document
        user_ = User.objects.get(username='rrosario')
        self.client.login(username='rrosario', password='testpass')
        response = post(self.client, 'wiki.document_vote',
                        {'not-helpful': 'No'}, args=[d.slug])
        eq_(200, response.status_code)
        votes = HelpfulVote.objects.filter(document=d, creator=user_)
        eq_(1, votes.count())
        assert not votes[0].helpful

    def test_vote_anonymous(self):
        """Test that voting works for anonymous user."""
        d = self.document
        response = post(self.client, 'wiki.document_vote',
                        {'helpful': 'Yes'}, args=[d.slug])
        eq_(200, response.status_code)
        votes = HelpfulVote.objects.filter(document=d, creator=None)
        votes = votes.exclude(anonymous_id=None)
        eq_(1, votes.count())
        assert votes[0].helpful

    def test_vote_ajax(self):
        """Test voting via ajax."""
        d = self.document
        url = reverse('wiki.document_vote', args=[d.slug])
        response = self.client.post(url, data={'helpful': 'Yes'},
                         HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        eq_(200, response.status_code)
        eq_('{"message": "Glad to hear it &mdash; thanks for the feedback!"}',
            response.content)
        votes = HelpfulVote.objects.filter(document=d, creator=None)
        votes = votes.exclude(anonymous_id=None)
        eq_(1, votes.count())
        assert votes[0].helpful


class SelectLocaleTests(TestCaseBase):
    """Test the locale selection page"""
    fixtures = ['users.json']

    def setUp(self):
        super(SelectLocaleTests, self).setUp()
        self.d = _create_document()
        self.client.login(username='admin', password='testpass')

    def test_page_renders_locales(self):
        """Load the page and verify it contains all the locales for l10n."""
        response = get(self.client, 'wiki.select_locale', args=[self.d.slug])
        eq_(200, response.status_code)
        doc = pq(response.content)
        eq_(len(settings.LANGUAGE_CHOICES) - 1,  # All except for 1 (en-US)
            len(doc('#select-locale ul.locales li')))


class RelatedDocumentTestCase(TestCaseBase):
    fixtures = ['users.json', 'wiki/documents.json']

    def test_related_order(self):
        calculate_related_documents()
        d = Document.uncached.get(pk=1)
        response = self.client.get(d.get_absolute_url())

        doc = pq(response.content)
        related = doc('section#related-articles li a')
        eq_(2, len(related))

        # If 'an article title 2' is first, the other must be second.
        eq_('an article title 2', related[0].text)


class RevisionDeleteTestCase(TestCaseBase):
    fixtures = ['users.json']

    def setUp(self):
        super(RevisionDeleteTestCase, self).setUp()
        self.d = _create_document()
        self.r = revision(document=self.d)
        self.r.save()

    def test_delete_revision_without_permissions(self):
        """Deleting a revision without permissions sends 403."""
        self.client.login(username='rrosario', password='testpass')
        response = get(self.client, 'wiki.delete_revision',
                       args=[self.d.slug, self.r.id])
        eq_(403, response.status_code)

        response = post(self.client, 'wiki.delete_revision',
                        args=[self.d.slug, self.r.id])
        eq_(403, response.status_code)

    def test_delete_revision_logged_out(self):
        """Deleting a revision while logged out redirects to login."""
        response = get(self.client, 'wiki.delete_revision',
                       args=[self.d.slug, self.r.id])
        redirect = response.redirect_chain[0]
        eq_(302, redirect[1])
        eq_('http://testserver/%s%s?next=/en-US/kb/%s/revision/%s/delete' %
            (settings.LANGUAGE_CODE, settings.LOGIN_URL, self.d.slug,
                self.r.id),
            redirect[0])

        response = post(self.client, 'wiki.delete_revision',
                        args=[self.d.slug, self.r.id])
        redirect = response.redirect_chain[0]
        eq_(302, redirect[1])
        eq_('http://testserver/%s%s?next=/en-US/kb/%s/revision/%s/delete' %
            (settings.LANGUAGE_CODE, settings.LOGIN_URL, self.d.slug,
                self.r.id),
            redirect[0])

    def test_delete_revision_with_permissions(self):
        """Deleting a revision with permissions should work."""
        self.client.login(username='admin', password='testpass')
        response = get(self.client, 'wiki.delete_revision',
                       args=[self.d.slug, self.r.id])
        eq_(200, response.status_code)

        response = post(self.client, 'wiki.delete_revision',
                        args=[self.d.slug, self.r.id])
        eq_(0, Revision.objects.filter(pk=self.r.id).count())

    def test_delete_current_revision(self):
        """Deleting a the current_revision of a document, should update
        the current_revision to previous version."""
        self.client.login(username='admin', password='testpass')
        prev_revision = self.d.current_revision
        prev_revision.reviewed = datetime.now() - timedelta(days=1)
        prev_revision.save()
        self.r.is_approved = True
        self.r.reviewed = datetime.now()
        self.r.save()
        d = Document.objects.get(pk=self.d.pk)
        eq_(self.r, d.current_revision)

        post(self.client, 'wiki.delete_revision',
             args=[self.d.slug, self.r.id])
        d = Document.objects.get(pk=d.pk)
        eq_(prev_revision, d.current_revision)

    def test_delete_only_revision(self):
        """If there is only one revision, it can't be deleted."""
        self.client.login(username='admin', password='testpass')

        # Create document with only 1 revision
        doc = document(save=True)
        rev = revision(document=doc, save=True)

        # Confirm page should show the message
        response = get(self.client, 'wiki.delete_revision',
                       args=[doc.slug, rev.id])
        eq_(200, response.status_code)
        eq_('Unable to delete only revision of the document',
            pq(response.content)('h1.title').text())

        # POST should return bad request and revision should still exist
        response = post(self.client, 'wiki.delete_revision',
                        args=[doc.slug, rev.id])
        eq_(400, response.status_code)
        Revision.uncached.get(id=rev.id)


class ApprovedWatchTests(TestCaseBase):
    """Tests for un/subscribing to revision approvals."""
    fixtures = ['users.json']

    def setUp(self):
        super(ApprovedWatchTests, self).setUp()
        self.client.login(username='rrosario', password='testpass')

    def test_watch_GET_405(self):
        """Watch with HTTP GET results in 405."""
        response = get(self.client, 'wiki.approved_watch')
        eq_(405, response.status_code)

    def test_unwatch_GET_405(self):
        """Unwatch with HTTP GET results in 405."""
        response = get(self.client, 'wiki.approved_unwatch')
        eq_(405, response.status_code)

    def test_watch_unwatch(self):
        """Watch and unwatch a document."""
        user_ = User.objects.get(username='rrosario')
        locale = 'es'

        # Subscribe
        response = post(self.client, 'wiki.approved_watch',
                        {'locale': locale})
        eq_(200, response.status_code)
        assert ApproveRevisionInLocaleEvent.is_notifying(user_, locale=locale)

        # Unsubscribe
        response = post(self.client, 'wiki.approved_unwatch',
                        {'locale': locale})
        eq_(200, response.status_code)
        assert not ApproveRevisionInLocaleEvent.is_notifying(user_,
                                                             locale=locale)


class DocumentDeleteTestCase(TestCaseBase):
    """Tests for document delete."""
    def setUp(self):
        super(DocumentDeleteTestCase, self).setUp()
        self.document = document(save=True)
        self.user = user(username='testuser', save=True)

    def test_delete_document_without_permissions(self):
        """Deleting a document without permissions sends 403."""
        self.client.login(username='testuser', password='testpass')
        response = get(self.client, 'wiki.document_delete',
                       args=[self.document.slug])
        eq_(403, response.status_code)

        response = post(self.client, 'wiki.document_delete',
                        args=[self.document.slug])
        eq_(403, response.status_code)

    def test_delete_document_logged_out(self):
        """Deleting a document while logged out redirects to login."""
        response = get(self.client, 'wiki.document_delete',
                       args=[self.document.slug])
        redirect = response.redirect_chain[0]
        eq_(302, redirect[1])
        eq_('http://testserver/%s%s?next=/en-US/kb/%s/delete' %
            (settings.LANGUAGE_CODE, settings.LOGIN_URL,
             self.document.slug),
            redirect[0])

        response = post(self.client, 'wiki.document_delete',
                        args=[self.document.slug])
        redirect = response.redirect_chain[0]
        eq_(302, redirect[1])
        eq_('http://testserver/%s%s?next=/en-US/kb/%s/delete' %
            (settings.LANGUAGE_CODE, settings.LOGIN_URL,
             self.document.slug),
            redirect[0])

    def _test_delete_document_with_permission(self):
        self.client.login(username='testuser', password='testpass')
        response = get(self.client, 'wiki.document_delete',
                       args=[self.document.slug])
        eq_(200, response.status_code)

        response = post(self.client, 'wiki.document_delete',
                        args=[self.document.slug])
        eq_(0, Document.objects.filter(pk=self.document.id).count())

    def test_document_with_l10n_permission(self):
        """Deleting a document with permissions should work."""
        add_permission(self.user, Document, 'delete_document_en-US')
        self._test_delete_document_with_permission()

    def test_revision_with_permission(self):
        """Deleting a document with delete_document permission should work."""
        add_permission(self.user, Document, 'delete_document')
        self._test_delete_document_with_permission()


# TODO: Merge with wiki.tests.doc_rev()?
def _create_document(title='Test Document', parent=None,
                     locale=settings.WIKI_DEFAULT_LANGUAGE):
    d = document(title=title, html='<div>Lorem Ipsum</div>',
                 category=10, locale=locale, parent=parent,
                 is_localizable=True)
    d.save()
    r = Revision(document=d, keywords='key1, key2', summary='lipsum',
                 content='<div>Lorem Ipsum</div>', creator_id=118577,
                 significance=SIGNIFICANCES[0][0], is_approved=True,
                 comment="Good job!")
    r.save()
    return d


def _translation_data():
    return {
        'title': 'Un Test Articulo', 'slug': 'un-test-articulo',
        'tags': 'tagUno,tagDos,tagTres',
        'keywords': 'keyUno, keyDos, keyTres',
        'summary': 'lipsumo',
        'content': 'loremo ipsumo doloro sito ameto'}
