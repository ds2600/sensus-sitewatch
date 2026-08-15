"""Custom field read/write helpers shared by routes/{sites,devices,
circuits}.py — the logic is identical regardless of object type, only
which CustomFieldDefinition rows apply differs (see models.py's
CustomFieldDefinition/CustomFieldValue docstrings for the data shape).
"""
from sitewatch.extensions import db
from sitewatch.models import CustomFieldDefinition, CustomFieldValue


def definitions_for(object_type):
    return CustomFieldDefinition.query.filter_by(object_type=object_type).order_by(CustomFieldDefinition.name).all()


def values_for(object_type, object_id):
    """{field_id: value} for one object — used to prefill an edit form and
    to render a detail page's custom fields list."""
    if object_id is None:
        return {}
    field_ids = [d.id for d in definitions_for(object_type)]
    if not field_ids:
        return {}
    rows = CustomFieldValue.query.filter(
        CustomFieldValue.object_id == object_id, CustomFieldValue.field_id.in_(field_ids)
    ).all()
    return {r.field_id: r.value for r in rows}


def set_values(object_type, object_id, form):
    """Reads custom_field_<id> inputs off the submitted form and upserts
    CustomFieldValue rows for every definition of this object_type — a
    blank submitted value CLEARS (deletes) that row rather than storing an
    empty string, so "never set" and "set to empty" don't get confused
    later. object_id must already be a real, flushed id (add routes need
    to db.session.flush() the new object first, same requirement
    audit_log.record() already has).

    Returns {"custom:<field name>": {"old":..., "new":...}} for whatever
    actually changed — the "custom:" prefix keeps these keys from ever
    colliding with the object's own regular field names in the same audit
    diff dict. Meant to be merged into the SAME audit_log.record() call as
    the rest of that object's edit, same pattern as circuits.py's
    waypoints diff — not a separate audit entry."""
    definitions = definitions_for(object_type)
    if not definitions:
        return {}
    existing = values_for(object_type, object_id)
    diff = {}
    for d in definitions:
        new_value = form.get(f"custom_field_{d.id}", "").strip()
        old_value = existing.get(d.id)
        if new_value == (old_value or ""):
            continue
        diff[f"custom:{d.name}"] = {"old": old_value, "new": new_value or None}
        row = CustomFieldValue.query.filter_by(field_id=d.id, object_id=object_id).first()
        if new_value:
            if row is None:
                db.session.add(CustomFieldValue(field_id=d.id, object_id=object_id, value=new_value))
            else:
                row.value = new_value
        elif row is not None:
            db.session.delete(row)
    return diff


def delete_values(object_type, object_id):
    """Called from a delete route BEFORE deleting the object itself —
    CustomFieldValue.object_id isn't a real FK, so nothing cascades here
    automatically (see that model's docstring)."""
    field_ids = [d.id for d in definitions_for(object_type)]
    if not field_ids:
        return
    CustomFieldValue.query.filter(
        CustomFieldValue.object_id == object_id, CustomFieldValue.field_id.in_(field_ids)
    ).delete(synchronize_session=False)
