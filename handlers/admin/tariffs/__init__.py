from aiogram import Router

from . import tariff_configurator, tariff_manage, tariff_sorting, tariff_subgroups

router = Router()

__all__ = (
    "router",
    "tariff_configurator",
    "tariff_manage",
    "tariff_sorting",
    "tariff_subgroups",
)
