# -*- coding: utf-8 -*-
# Generated by Django 1.11.26 on 2019-12-10 22:06
from __future__ import unicode_literals

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('couchforms', '0006_unfinishedarchivestub_attempts'),
    ]

    operations = [
        migrations.AlterField(
            model_name='unfinishedarchivestub',
            name='xform_id',
            field=models.CharField(max_length=200, unique=True),
        ),
    ]
