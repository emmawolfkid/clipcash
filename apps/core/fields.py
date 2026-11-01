from django.db import models

from .money import MAX_AMOUNT, validate_amount, validate_non_negative_amount


class AmountField(models.BigIntegerField):
    """
    Integer money amount in whole shillings (see apps.core.money).

    Pair it with a currency field or a fixed currency, and add a database
    CheckConstraint for sign rules on the owning model: validators only run in
    full_clean(), constraints always run.
    """

    def __init__(self, *args, non_negative=False, **kwargs):
        self.non_negative = non_negative
        validator = validate_non_negative_amount if non_negative else validate_amount
        kwargs.setdefault("validators", [validator])
        super().__init__(*args, **kwargs)

    def deconstruct(self):
        name, path, args, kwargs = super().deconstruct()
        if self.non_negative:
            kwargs["non_negative"] = True
        kwargs.pop("validators", None)
        return name, path, args, kwargs

    def formfield(self, **kwargs):
        bound = 0 if self.non_negative else -MAX_AMOUNT
        return super().formfield(**{"min_value": bound, "max_value": MAX_AMOUNT, **kwargs})
