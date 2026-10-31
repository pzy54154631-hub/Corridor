"""Synthetic showcase-only partner contact and birthday added to the interactive UI state.

The frozen runner fixture is unchanged. These objects are ordinary synthetic state:
the relation shown for them is read from trusted aliases at display time and never
enters an authorization.
"""

from corridor.schemas.domain import CalendarEvent, Contact
from corridor.schemas.state import BankState

SPOUSE_CONTACT_REF = "CONTACT_SPOUSE"
SPOUSE_BIRTHDAY_REF = "BIRTHDAY_SPOUSE"


def with_spouse_birthday(state: BankState) -> BankState:
    """Add only new objects; an existing identity is never replaced."""
    if SPOUSE_CONTACT_REF in state.contacts or SPOUSE_BIRTHDAY_REF in state.calendar:
        raise ValueError("The showcase overlay only adds new synthetic objects")
    contact = Contact(
        id=SPOUSE_CONTACT_REF,
        owner="C_DEMO",
        name_token="NAME_SPOUSE",
        phone_token="PHONE_SPOUSE",
        aliases=["爱人", "老婆", "SPOUSE"],
        destination_ref="DEST_CONTACT_SPOUSE",
        destination_version=1,
        verified_at=state.now,
        verification_ref="VERIFY_CONTACT_SPOUSE",
        is_new=False,
    )
    event = CalendarEvent(
        id=SPOUSE_BIRTHDAY_REF,
        owner="C_DEMO",
        event_type="birthday",
        event_date="2026-10-24",
        person_contact_ref=SPOUSE_CONTACT_REF,
        source_ref="DEVICE_CAL",
    )
    return state.model_copy(
        update={
            "contacts": {**state.contacts, contact.id: contact},
            "calendar": {**state.calendar, event.id: event},
        }
    )
