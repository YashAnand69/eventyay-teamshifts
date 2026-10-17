import json
from datetime import timedelta

import pytest
from django.urls import reverse
from django.utils.timezone import now
from django_scopes import scope
from eventyay.base.models import Team, User

from teamshifts.models import Shift, ShiftAssignment, ShiftLocation, ShiftRoleAssignment, TeamRole


@pytest.fixture
def role_setup(client, event, user, settings):
    settings.SITE_URL = "https://testserver"
    with scope(event=event):
        allowed = TeamRole.objects.create(event=event, name="Registration")
        protected = TeamRole.objects.create(event=event, name="Security")
        team = Team.objects.create(
            organizer=event.organizer,
            name="Registration lead",
            all_events=True,
            teamshifts_role="lead",
            all_teamshifts_roles=False,
            limit_teamshifts_roles=[allowed.pk],
        )
        team.members.add(user)
        location = ShiftLocation.objects.create(event=event, name="Hall")
        shift = Shift.objects.create(event=event, name="Morning", location=location, start_time=now(), end_time=now() + timedelta(hours=1))
        editable = ShiftRoleAssignment.objects.create(shift=shift, role=allowed, capacity=2)
        locked = ShiftRoleAssignment.objects.create(shift=shift, role=protected, capacity=3)
        volunteer = User.objects.create_user(email="volunteer@example.com", password="secret")
        assignment = ShiftAssignment.objects.create(shift=shift, role=protected, team_member=volunteer)
    client.force_login(user)
    return client, event, shift, allowed, protected, editable, locked, assignment, team


def url(event, name, shift=None):
    kwargs = {"organizer": event.organizer.slug, "event": event.slug}
    if shift is not None:
        kwargs["pk"] = shift.pk
    return reverse("plugins:teamshifts:" + name, kwargs=kwargs)


def assert_protected(setup):
    _, event, shift, _, protected, _, locked, assignment, _ = setup
    with scope(event=event):
        assert Shift.objects.filter(pk=shift.pk).exists()
        locked.refresh_from_db()
        assert locked.role_id == protected.pk
        assert locked.capacity == 3
        assert ShiftAssignment.objects.filter(pk=assignment.pk, role=protected).exists()


@pytest.mark.django_db
@pytest.mark.parametrize("endpoint", ["api_shift_detail", "public_shift_manage"])
@pytest.mark.parametrize("operation", ["change", "add"])
def test_limited_lead_cannot_modify_other_roles(role_setup, endpoint, operation):
    client, event, shift, allowed, protected, *_ = role_setup
    with scope(event=event):
        role = protected if operation == "change" else TeamRole.objects.create(event=event, name="Catering")
    payload = {"title": {"en": "Must not change"}, "roles": [{"id": allowed.pk, "capacity": 4}, {"id": role.pk, "capacity": 9}]}
    response = client.patch(url(event, endpoint, shift), data=json.dumps(payload), content_type="application/json")
    assert response.status_code == 403
    with scope(event=event):
        shift.refresh_from_db()
        assert shift.name == "Morning"
        assert shift.role_assignments.get(role=allowed).capacity == 2
    assert_protected(role_setup)


@pytest.mark.django_db
@pytest.mark.parametrize("endpoint", ["api_shift_detail", "public_shift_manage"])
@pytest.mark.parametrize("legacy", [False, True])
def test_limited_lead_edits_own_role_without_removing_other_roles(role_setup, endpoint, legacy):
    client, event, shift, allowed, *_ = role_setup
    payload = {"role": allowed.pk, "capacity": 4} if legacy else {"roles": [{"id": allowed.pk, "capacity": 4}]}
    response = client.patch(url(event, endpoint, shift), data=json.dumps(payload), content_type="application/json")
    assert response.status_code == 200
    with scope(event=event):
        assert shift.role_assignments.get(role=allowed).capacity == 4
    assert_protected(role_setup)


@pytest.mark.django_db
@pytest.mark.parametrize("endpoint", ["api_shift_detail", "public_shift_manage", "shift_delete", "shift_bulk_delete"])
def test_limited_lead_cannot_delete_mixed_role_shift(role_setup, endpoint):
    client, event, shift, *_ = role_setup
    if endpoint == "shift_bulk_delete":
        response = client.post(url(event, endpoint), {"shift_ids": [shift.pk]})
    elif endpoint == "shift_delete":
        response = client.post(url(event, endpoint, shift))
    else:
        response = client.delete(url(event, endpoint, shift))
    assert response.status_code == 403
    assert_protected(role_setup)


@pytest.mark.django_db
@pytest.mark.parametrize("endpoint", ["api_shift_detail", "public_shift_manage", "shift_delete", "shift_bulk_delete"])
def test_unrestricted_lead_can_delete_mixed_role_shift(role_setup, endpoint):
    client, event, shift, *_, team = role_setup
    team.all_teamshifts_roles = True
    team.save(update_fields=["all_teamshifts_roles"])
    if endpoint == "shift_bulk_delete":
        response = client.post(url(event, endpoint), {"shift_ids": [shift.pk]})
    elif endpoint == "shift_delete":
        response = client.post(url(event, endpoint, shift))
    else:
        response = client.delete(url(event, endpoint, shift))
    assert response.status_code in (200, 302)
    with scope(event=event):
        assert not Shift.objects.filter(pk=shift.pk).exists()


@pytest.mark.django_db
def test_classic_form_only_edits_allowed_roles(role_setup):
    client, event, shift, allowed, protected, editable, _, _, _ = role_setup
    response = client.get(url(event, "shift_edit", shift))
    formset = response.context["formset"]
    assert [form.instance.pk for form in formset.initial_forms] == [editable.pk]
    assert all(protected not in form.fields["role"].queryset for form in formset.forms)
    data = {
        "mode": "single",
        "name": "Updated",
        "location": shift.location_id,
        "start_time": shift.start_time.strftime("%Y-%m-%dT%H:%M"),
        "end_time": shift.end_time.strftime("%Y-%m-%dT%H:%M"),
        "roles-TOTAL_FORMS": 1,
        "roles-INITIAL_FORMS": 1,
        "roles-0-id": editable.pk,
        "roles-0-role": allowed.pk,
        "roles-0-capacity": 4,
    }
    response = client.post(url(event, "shift_edit", shift), data)
    assert response.status_code == 302
    assert_protected(role_setup)


@pytest.mark.django_db
def test_classic_form_rejects_forged_protected_assignment(role_setup):
    client, event, shift, allowed, _, _, locked, _, _ = role_setup
    data = {
        "mode": "single",
        "name": "Must not change",
        "location": shift.location_id,
        "start_time": shift.start_time.strftime("%Y-%m-%dT%H:%M"),
        "end_time": shift.end_time.strftime("%Y-%m-%dT%H:%M"),
        "roles-TOTAL_FORMS": 1,
        "roles-INITIAL_FORMS": 1,
        "roles-0-id": locked.pk,
        "roles-0-role": allowed.pk,
        "roles-0-capacity": 4,
        "roles-0-DELETE": "on",
    }
    response = client.post(url(event, "shift_edit", shift), data)
    assert response.status_code == 403
    assert_protected(role_setup)


@pytest.mark.django_db
def test_editor_role_picker_only_lists_allowed_roles(role_setup):
    client, event, _, allowed, *_ = role_setup
    response = client.get(url(event, "api_shifts"))
    assert response.status_code == 200
    assert [role["id"] for role in response.json()["roles"]] == [allowed.pk]


@pytest.mark.django_db
def test_limited_lead_can_remove_last_editable_role(role_setup):
    client, event, shift, allowed, _, editable, *_ = role_setup
    data = {
        "mode": "single",
        "name": "Updated",
        "location": shift.location_id,
        "start_time": shift.start_time.strftime("%Y-%m-%dT%H:%M"),
        "end_time": shift.end_time.strftime("%Y-%m-%dT%H:%M"),
        "roles-TOTAL_FORMS": 1,
        "roles-INITIAL_FORMS": 1,
        "roles-0-id": editable.pk,
        "roles-0-role": allowed.pk,
        "roles-0-capacity": 4,
        "roles-0-DELETE": "on",
    }
    response = client.post(url(event, "shift_edit", shift), data)
    assert response.status_code == 302
    with scope(event=event):
        assert not shift.role_assignments.filter(role=allowed).exists()
    assert_protected(role_setup)


@pytest.mark.django_db
def test_api_create_rejects_unmanaged_role_without_creating_shift(role_setup):
    client, event, shift, _, protected, *_ = role_setup
    payload = {"start": shift.start_time.isoformat(), "end": shift.end_time.isoformat(), "roles": [{"id": protected.pk, "capacity": 1}]}
    response = client.post(url(event, "api_shifts"), data=json.dumps(payload), content_type="application/json")
    assert response.status_code == 403
    with scope(event=event):
        assert list(Shift.objects.values_list("pk", flat=True)) == [shift.pk]


@pytest.mark.django_db
def test_bulk_delete_is_all_or_nothing(role_setup):
    client, event, shift, allowed, *_ = role_setup
    with scope(event=event):
        editable_shift = Shift.objects.create(event=event, name="Editable", start_time=shift.start_time, end_time=shift.end_time)
        ShiftRoleAssignment.objects.create(shift=editable_shift, role=allowed, capacity=1)
    response = client.post(url(event, "shift_bulk_delete"), {"shift_ids": [editable_shift.pk, shift.pk]})
    assert response.status_code == 403
    with scope(event=event):
        assert Shift.objects.filter(pk=editable_shift.pk).exists()
    assert_protected(role_setup)


@pytest.mark.django_db
@pytest.mark.parametrize("endpoint", ["api_shift_detail", "public_shift_manage"])
def test_unrestricted_lead_can_change_all_roles(role_setup, endpoint):
    client, event, shift, allowed, protected, *_, team = role_setup
    team.all_teamshifts_roles = True
    team.save(update_fields=["all_teamshifts_roles"])
    response = client.patch(url(event, endpoint, shift), data=json.dumps({"roles": [{"id": protected.pk, "capacity": 5}]}), content_type="application/json")
    assert response.status_code == 200
    with scope(event=event):
        assert shift.role_assignments.get(role=protected).capacity == 5
        assert not shift.role_assignments.filter(role=allowed).exists()


@pytest.mark.django_db
def test_duplicate_form_only_prefills_allowed_roles(role_setup):
    client, event, shift, allowed, protected, *_ = role_setup
    response = client.get(url(event, "shift_duplicate", shift))
    assert response.status_code == 200
    assert [form.initial["role"] for form in response.context["formset"].forms] == [allowed.pk]
    assert all(protected not in form.fields["role"].queryset for form in response.context["formset"].forms)


@pytest.mark.django_db
@pytest.mark.parametrize("endpoint", ["api_shift_detail", "public_shift_manage", "shift_delete", "shift_bulk_delete"])
def test_limited_lead_can_delete_shift_with_only_allowed_roles(role_setup, endpoint):
    client, event, shift, _, protected, *_ = role_setup
    with scope(event=event):
        shift.assignments.filter(role=protected).delete()
        shift.role_assignments.filter(role=protected).delete()
    if endpoint == "shift_bulk_delete":
        response = client.post(url(event, endpoint), {"shift_ids": [shift.pk]})
    elif endpoint == "shift_delete":
        response = client.post(url(event, endpoint, shift))
    else:
        response = client.delete(url(event, endpoint, shift))
    assert response.status_code in (200, 302)
    with scope(event=event):
        assert not Shift.objects.filter(pk=shift.pk).exists()


@pytest.mark.django_db
@pytest.mark.parametrize("endpoint", ["api_shift_detail", "public_shift_manage"])
def test_unchanged_unmanaged_roles_keep_their_identity(role_setup, endpoint):
    client, event, shift, allowed, protected, *_ = role_setup
    response = client.patch(
        url(event, endpoint, shift),
        data=json.dumps({"roles": [{"id": allowed.pk, "capacity": 4}, {"id": protected.pk, "capacity": 3}]}),
        content_type="application/json",
    )
    assert response.status_code == 200
    assert_protected(role_setup)
