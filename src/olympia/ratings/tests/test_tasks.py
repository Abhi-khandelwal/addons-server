# -*- coding: utf-8 -*-
from unittest import mock
import pytest
from waffle.testutils import override_switch

from django.conf import settings

from olympia.amo.tests import TestCase, addon_factory, user_factory
from olympia.lib.akismet.models import AkismetReport
from olympia.ratings.models import Rating, RatingFlag
from olympia.ratings.tasks import (
    addon_rating_aggregates, check_akismet_reports)


class TestAddonRatingAggregates(TestCase):
    # Prevent Rating.post_save() from being fired when setting up test data,
    # since it'd call addon_rating_aggregates too early.
    @mock.patch.object(Rating, 'post_save', lambda *args, **kwargs: None)
    def test_addon_rating_aggregates(self):
        addon = addon_factory()
        addon2 = addon_factory()

        # Add a purely unlisted add-on. It should not be considered when
        # calculating bayesian rating for the other add-ons.
        addon3 = addon_factory(total_ratings=3, average_rating=4)
        self.make_addon_unlisted(addon3)

        # Create a few ratings with various scores.
        user = user_factory()
        # Add an old rating that should not be used to calculate the average,
        # because the same user posts a new one right after that.
        old_rating = Rating.objects.create(
            addon=addon, rating=1, user=user, is_latest=False, body=u'old')
        new_rating = Rating.objects.create(addon=addon, rating=3, user=user,
                                           body=u'new')
        Rating.objects.create(addon=addon, rating=3, user=user_factory(),
                              body=u'foo')
        Rating.objects.create(addon=addon, rating=2, user=user_factory())
        Rating.objects.create(addon=addon, rating=1, user=user_factory())

        # On another addon as well.
        Rating.objects.create(addon=addon2, rating=1, user=user_factory())
        Rating.objects.create(addon=addon2, rating=1, user=user_factory(),
                              body=u'two')

        # addon_rating_aggregates should ignore replies, so let's add one.
        Rating.objects.create(
            addon=addon, rating=5, user=user_factory(), reply_to=new_rating)

        # Make sure old_review is considered old, new_review considered new.
        old_rating.reload()
        new_rating.reload()
        assert old_rating.is_latest is False
        assert new_rating.is_latest is True

        # Make sure total_ratings hasn't been updated yet (because we are
        # mocking post_save()).
        addon.reload()
        addon2.reload()
        assert addon.total_ratings == 0
        assert addon2.total_ratings == 0
        assert addon.bayesian_rating == 0
        assert addon.average_rating == 0
        assert addon2.bayesian_rating == 0
        assert addon2.average_rating == 0
        assert addon.text_ratings_count == 0
        assert addon2.text_ratings_count == 0

        # Trigger the task and test results.
        addon_rating_aggregates([addon.pk, addon2.pk])
        addon.reload()
        addon2.reload()
        assert addon.total_ratings == 4
        assert addon2.total_ratings == 2
        assert addon.bayesian_rating == 1.9821428571428572
        assert addon.average_rating == 2.25
        assert addon2.bayesian_rating == 1.375
        assert addon2.average_rating == 1.0
        assert addon.text_ratings_count == 2
        assert addon2.text_ratings_count == 1

        # Trigger the task with a single add-on.
        Rating.objects.create(addon=addon2, rating=5, user=user_factory(),
                              body=u'xxx')
        addon2.reload()
        assert addon2.total_ratings == 2

        addon_rating_aggregates(addon2.pk)
        addon2.reload()
        assert addon2.total_ratings == 3
        assert addon2.text_ratings_count == 2
        assert addon.bayesian_rating == 1.9821428571428572
        assert addon.average_rating == 2.25
        assert addon2.bayesian_rating == 1.97915
        assert addon2.average_rating == 2.3333


@pytest.mark.django_db
@pytest.mark.parametrize(
    'return_value,headers,waffle_on,flag_count',
    [
        (True, {}, True, 1),
        (True, {'X-akismet-pro-tip': 'discard'}, True, 1),
        (False, {}, True, 0),
        # when the akismet-rating-action is off there shouldn't be any flagging
        (True, {}, False, 0),
        (True, {'X-akismet-pro-tip': 'discard'}, False, 0),
    ])
def test_check_akismet_reports(return_value, headers, waffle_on, flag_count):
    task_user = user_factory(id=settings.TASK_USER_ID)
    assert RatingFlag.objects.count() == 0
    rating = Rating.objects.create(
        addon=addon_factory(), user=user_factory(), rating=4, body=u'spám?',
        ip_address='1.2.3.4')
    akismet_report = AkismetReport.create_for_rating(rating, 'foo/baa', '')

    with mock.patch('olympia.lib.akismet.models.requests.post') as post_mock:
        # Mock a definitely spam response - same outcome
        post_mock.return_value.json.return_value = return_value
        post_mock.return_value.headers = headers
        with override_switch('akismet-rating-action', active=waffle_on):
            check_akismet_reports([akismet_report.id])

    RatingFlag.objects.count() == flag_count
    rating = rating.reload()
    if flag_count > 0:
        flag = RatingFlag.objects.get()
        assert flag.rating == rating
        assert flag.user == task_user
        assert flag.flag == RatingFlag.SPAM
        assert rating.editorreview
    else:
        assert not rating.editorreview
