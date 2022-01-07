# Generated by Django 2.2.24 on 2021-12-20 14:22

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('sponsors', '0062_auto_20211111_1529'),
    ]

    operations = [
        migrations.AddField(
            model_name='sponsorshipbenefit',
            name='a_la_carte',
            field=models.BooleanField(default=False, help_text='À la carte benefits can be selected without the need of a package.', verbose_name='À La Carte'),
        ),
        migrations.AlterField(
            model_name='requiredtextasset',
            name='label',
            field=models.CharField(help_text="What's the title used to display the text input to the sponsor?", max_length=256),
        ),
        migrations.AlterField(
            model_name='requiredtextassetconfiguration',
            name='label',
            field=models.CharField(help_text="What's the title used to display the text input to the sponsor?", max_length=256),
        ),
    ]