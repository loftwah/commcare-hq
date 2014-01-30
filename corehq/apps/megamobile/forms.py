from django.forms.fields import *
from corehq.apps.sms.forms import BackendForm
from dimagi.utils.django.fields import TrimmedCharField

class MegamobileBackendForm(BackendForm):
    default_pid = TrimmedCharField()

