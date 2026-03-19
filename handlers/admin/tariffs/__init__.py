from aiogram import Router

router = Router()

from . import tariff_configurator, tariff_manage, tariff_sorting, tariff_subgroups

__all__ = (
    "router",
    "tariff_configurator",
    "tariff_manage",
    "tariff_sorting",
    "tariff_subgroups",
)
