"""
This module holds models related to the Sponsorship entity.
"""
from datetime import date
from itertools import chain

from django.conf import settings
from django.contrib.contenttypes.fields import GenericRelation
from django.core.exceptions import ObjectDoesNotExist
from django.db import models, transaction
from django.db.models import Subquery, Sum
from django.template.defaultfilters import truncatechars
from django.urls import reverse
from django.utils import timezone
from django.utils.functional import cached_property
from num2words import num2words

from ordered_model.models import OrderedModel

from sponsors.exceptions import SponsorWithExistingApplicationException, InvalidStatusException, \
    SponsorshipInvalidDateRangeException
from sponsors.models.assets import GenericAsset
from sponsors.models.managers import SponsorshipPackageManager, SponsorshipBenefitManager, SponsorshipQuerySet
from sponsors.models.benefits import TieredQuantityConfiguration
from sponsors.models.sponsors import SponsorBenefit


class SponsorshipPackage(OrderedModel):
    """
    Represent default packages of benefits (visionary, sustainability etc)
    """
    objects = SponsorshipPackageManager()

    name = models.CharField(max_length=64)
    sponsorship_amount = models.PositiveIntegerField()
    advertise = models.BooleanField(default=False, blank=True, help_text="If checked, this package will be advertised "
                                                                         "in the sponsosrhip application")
    logo_dimension = models.PositiveIntegerField(default=175, blank=True, help_text="Internal value used to control "
                                                                                    "logos dimensions at sponsors "
                                                                                    "page")
    slug = models.SlugField(db_index=True, blank=False, null=False, help_text="Internal identifier used "
                                                                              "to reference this package.")

    def __str__(self):
        return self.name

    class Meta(OrderedModel.Meta):
        pass

    def has_user_customization(self, benefits):
        """
        Given a list of benefits this method checks if it exclusively matches the sponsor package benefits
        """
        pkg_benefits_with_conflicts = set(self.benefits.with_conflicts())

        # check if all packages' benefits without conflict are present in benefits list
        from_pkg_benefits = {
            b for b in benefits if b not in pkg_benefits_with_conflicts
        }
        if from_pkg_benefits != set(self.benefits.without_conflicts()):
            return True

        # check if at least one of the conflicting benefits is present
        remaining_benefits = set(benefits) - from_pkg_benefits
        if not remaining_benefits and pkg_benefits_with_conflicts:
            return True

        # create groups of conflicting benefits ids
        conflicts_groups = []
        for pkg_benefit in pkg_benefits_with_conflicts:
            if pkg_benefit in chain(*conflicts_groups):
                continue
            grp = set([pkg_benefit] + list(pkg_benefit.conflicts.all()))
            conflicts_groups.append(grp)

        has_all_conflicts = all(
            g.intersection(remaining_benefits) for g in conflicts_groups
        )
        return not has_all_conflicts

    def get_user_customization(self, benefits):
        """
        Given a list of benefits this method returns the customizations
        """
        benefits = set(tuple(benefits))
        pkg_benefits = set(tuple(self.benefits.all()))
        return {
          "added_by_user": benefits - pkg_benefits,
          "removed_by_user": pkg_benefits - benefits,
        }


class SponsorshipProgram(OrderedModel):
    """
    Possible programs that a benefit belongs to (Foundation, Pypi, etc)
    """

    name = models.CharField(max_length=64)
    description = models.TextField(null=True, blank=True)

    def __str__(self):
        return self.name

    class Meta(OrderedModel.Meta):
        pass


class Sponsorship(models.Model):
    """
    Represente a sponsorship application by a sponsor.
    It's responsible to group the set of selected benefits and
    link it to sponsor
    """

    APPLIED = "applied"
    REJECTED = "rejected"
    APPROVED = "approved"
    FINALIZED = "finalized"

    STATUS_CHOICES = [
        (APPLIED, "Applied"),
        (REJECTED, "Rejected"),
        (APPROVED, "Approved"),
        (FINALIZED, "Finalized"),
    ]

    objects = SponsorshipQuerySet.as_manager()

    submited_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL
    )
    sponsor = models.ForeignKey("Sponsor", null=True, on_delete=models.SET_NULL)
    status = models.CharField(
        max_length=20, choices=STATUS_CHOICES, default=APPLIED, db_index=True
    )

    start_date = models.DateField(null=True, blank=True)
    end_date = models.DateField(null=True, blank=True)
    applied_on = models.DateField(auto_now_add=True)
    approved_on = models.DateField(null=True, blank=True)
    rejected_on = models.DateField(null=True, blank=True)
    finalized_on = models.DateField(null=True, blank=True)

    for_modified_package = models.BooleanField(
        default=False,
        help_text="If true, it means the user customized the package's benefits. Changes are listed under section 'User Customizations'.",
    )
    level_name_old = models.CharField(max_length=64, default="", blank=True, help_text="DEPRECATED: shall be removed "
                                                                                       "after manual data sanity "
                                                                                       "check.", verbose_name="Level "
                                                                                                              "name")
    package = models.ForeignKey(SponsorshipPackage, null=True, on_delete=models.SET_NULL)
    sponsorship_fee = models.PositiveIntegerField(null=True, blank=True)
    overlapped_by = models.ForeignKey("self", null=True, on_delete=models.SET_NULL)

    assets = GenericRelation(GenericAsset)

    class Meta:
        permissions = [
            ("sponsor_publisher", "Can access sponsor placement API"),
        ]

    @property
    def level_name(self):
        return self.package.name if self.package else self.level_name_old

    @level_name.setter
    def level_name(self, value):
        self.level_name_old = value

    @cached_property
    def user_customizations(self):
        benefits = [b.sponsorship_benefit for b in self.benefits.select_related("sponsorship_benefit")]
        return self.package.get_user_customization(benefits)

    def __str__(self):
        repr = f"{self.level_name} ({self.get_status_display()}) for sponsor {self.sponsor.name}"
        if self.start_date and self.end_date:
            fmt = "%m/%d/%Y"
            start = self.start_date.strftime(fmt)
            end = self.end_date.strftime(fmt)
            repr += f" [{start} - {end}]"
        return repr

    @classmethod
    @transaction.atomic
    def new(cls, sponsor, benefits, package=None, submited_by=None):
        """
        Creates a Sponsorship with a Sponsor and a list of SponsorshipBenefit.
        This will create SponsorBenefit copies from the benefits
        """
        for_modified_package = False
        package_benefits = []

        if package and package.has_user_customization(benefits):
            package_benefits = package.benefits.all()
            for_modified_package = True
        elif not package:
            for_modified_package = True

        if cls.objects.in_progress().filter(sponsor=sponsor).exists():
            raise SponsorWithExistingApplicationException(f"Sponsor pk: {sponsor.pk}")

        sponsorship = cls.objects.create(
            submited_by=submited_by,
            sponsor=sponsor,
            level_name="" if not package else package.name,
            package=package,
            sponsorship_fee=None if not package else package.sponsorship_amount,
            for_modified_package=for_modified_package,
        )

        for benefit in benefits:
            added_by_user = for_modified_package and benefit not in package_benefits
            SponsorBenefit.new_copy(
                benefit, sponsorship=sponsorship, added_by_user=added_by_user
            )

        return sponsorship

    @property
    def estimated_cost(self):
        return (
                self.benefits.aggregate(Sum("benefit_internal_value"))[
                    "benefit_internal_value__sum"
                ]
                or 0
        )

    @property
    def verbose_sponsorship_fee(self):
        if self.sponsorship_fee is None:
            return 0
        return num2words(self.sponsorship_fee)

    @property
    def agreed_fee(self):
        valid_status = [Sponsorship.APPROVED, Sponsorship.FINALIZED]
        if self.status in valid_status:
            return self.sponsorship_fee
        try:
            benefits = [sb.sponsorship_benefit for sb in self.package_benefits.all().select_related('sponsorship_benefit')]
            if self.package and not self.package.has_user_customization(benefits):
                return self.sponsorship_fee
        except SponsorshipPackage.DoesNotExist:  # sponsorship level names can change over time
            return None

    @property
    def is_active(self):
        conditions = [
            self.status == self.FINALIZED,
            self.end_date and self.end_date > date.today()
        ]

    def reject(self):
        if self.REJECTED not in self.next_status:
            msg = f"Can't reject a {self.get_status_display()} sponsorship."
            raise InvalidStatusException(msg)
        self.status = self.REJECTED
        self.rejected_on = timezone.now().date()

    def approve(self, start_date, end_date):
        if self.APPROVED not in self.next_status:
            msg = f"Can't approve a {self.get_status_display()} sponsorship."
            raise InvalidStatusException(msg)
        if start_date >= end_date:
            msg = f"Start date greater or equal than end date"
            raise SponsorshipInvalidDateRangeException(msg)
        self.status = self.APPROVED
        self.start_date = start_date
        self.end_date = end_date
        self.approved_on = timezone.now().date()

    def rollback_to_editing(self):
        accepts_rollback = [self.APPLIED, self.APPROVED, self.REJECTED]
        if self.status not in accepts_rollback:
            msg = f"Can't rollback to edit a {self.get_status_display()} sponsorship."
            raise InvalidStatusException(msg)

        try:
            if not self.contract.is_draft:
                status = self.contract.get_status_display()
                msg = f"Can't rollback to edit a sponsorship with a { status } Contract."
                raise InvalidStatusException(msg)
            self.contract.delete()
        except ObjectDoesNotExist:
            pass

        self.status = self.APPLIED
        self.approved_on = None
        self.rejected_on = None

    @property
    def verified_emails(self):
        emails = [self.submited_by.email]
        if self.sponsor:
            emails = self.sponsor.verified_emails(initial_emails=emails)
        return emails

    @property
    def admin_url(self):
        return reverse("admin:sponsors_sponsorship_change", args=[self.pk])

    @property
    def contract_admin_url(self):
        if not self.contract:
            return ""
        return reverse(
            "admin:sponsors_contract_change", args=[self.contract.pk]
        )

    @property
    def detail_url(self):
        return reverse("users:sponsorship_application_detail", args=[self.pk])

    @cached_property
    def package_benefits(self):
        return self.benefits.filter(added_by_user=False)

    @cached_property
    def added_benefits(self):
        return self.benefits.filter(added_by_user=True)

    @property
    def open_for_editing(self):
        return self.status == self.APPLIED

    @property
    def next_status(self):
        states_map = {
            self.APPLIED: [self.APPROVED, self.REJECTED],
            self.APPROVED: [self.FINALIZED],
            self.REJECTED: [],
            self.FINALIZED: [],
        }
        return states_map[self.status]


class SponsorshipBenefit(OrderedModel):
    """
    Benefit that sponsors can pick which are organized under
    package and program.
    """

    objects = SponsorshipBenefitManager()

    # Public facing
    name = models.CharField(
        max_length=1024,
        verbose_name="Benefit Name",
        help_text="For display in the application form, contract, and sponsor dashboard.",
    )
    description = models.TextField(
        null=True,
        blank=True,
        verbose_name="Benefit Description",
        help_text="For display on generated prospectuses and the website.",
    )
    program = models.ForeignKey(
        SponsorshipProgram,
        null=False,
        blank=False,
        on_delete=models.CASCADE,
        verbose_name="Sponsorship Program",
        help_text="Which sponsorship program the benefit is associated with.",
    )
    packages = models.ManyToManyField(
        SponsorshipPackage,
        related_name="benefits",
        verbose_name="Sponsorship Packages",
        help_text="What sponsorship packages this benefit is included in.",
        blank=True,
    )
    package_only = models.BooleanField(
        default=False,
        verbose_name="Sponsor Package Only Benefit",
        help_text="If a benefit is only available via a sponsorship package, select this option.",
    )
    new = models.BooleanField(
        default=False,
        verbose_name="New Benefit",
        help_text='If selected, display a "New This Year" badge along side the benefit.',
    )
    unavailable = models.BooleanField(
        default=False,
        verbose_name="Benefit is unavailable",
        help_text="If selected, this benefit will not be available to applicants.",
    )
    a_la_carte = models.BooleanField(
        default=False,
        verbose_name="À La Carte",
        help_text="À la carte benefits can be selected without the need of a package.",
    )

    # Internal
    legal_clauses = models.ManyToManyField(
        "LegalClause",
        related_name="benefits",
        verbose_name="Legal Clauses",
        help_text="Legal clauses to be displayed in the contract",
        blank=True,
    )
    internal_description = models.TextField(
        null=True,
        blank=True,
        verbose_name="Internal Description or Notes",
        help_text="Any description or notes for internal use.",
    )
    internal_value = models.PositiveIntegerField(
        null=True,
        blank=True,
        verbose_name="Internal Value",
        help_text=(
            "Value used internally to calculate sponsorship value when applicants "
            "construct their own sponsorship packages."
        ),
    )
    capacity = models.PositiveIntegerField(
        null=True,
        blank=True,
        verbose_name="Capacity",
        help_text="For benefits with limited capacity, set it here.",
    )
    soft_capacity = models.BooleanField(
        default=False,
        verbose_name="Soft Capacity",
        help_text="If a benefit's capacity is flexible, select this option.",
    )
    conflicts = models.ManyToManyField(
        "self",
        blank=True,
        symmetrical=True,
        verbose_name="Conflicts",
        help_text="For benefits that conflict with one another,",
    )

    NEW_MESSAGE = "New benefit this year!"
    PACKAGE_ONLY_MESSAGE = "Benefit only available as part of a sponsor package"
    NO_CAPACITY_MESSAGE = "This benefit is currently at capacity"

    @property
    def unavailability_message(self):
        if self.package_only:
            return self.PACKAGE_ONLY_MESSAGE
        if not self.has_capacity:
            return self.NO_CAPACITY_MESSAGE
        return ""

    @property
    def has_capacity(self):
        if self.unavailable:
            return False
        return not (
            self.remaining_capacity is not None
            and self.remaining_capacity <= 0
            and not self.soft_capacity
        )

    @property
    def remaining_capacity(self):
        # TODO implement logic to compute
        return self.capacity

    @property
    def features_config(self):
        return self.benefitfeatureconfiguration_set

    @property
    def related_sponsorships(self):
        ids_qs = self.sponsorbenefit_set.values_list("sponsorship__pk", flat=True)
        return Sponsorship.objects.filter(id__in=Subquery(ids_qs))

    def __str__(self):
        return f"{self.program} > {self.name}"

    def _short_name(self):
        return truncatechars(self.name, 42)

    def name_for_display(self, package=None):
        name = self.name
        for feature in self.features_config.all():
            name = feature.display_modifier(name, package=package)
        return name

    _short_name.short_description = "Benefit Name"
    short_name = property(_short_name)

    @cached_property
    def has_tiers(self):
        return self.features_config.instance_of(TieredQuantityConfiguration).count() > 0

    class Meta(OrderedModel.Meta):
        pass
