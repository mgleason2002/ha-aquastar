"""Config flow for Town of Cary Aquastar integration."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.config_entries import ConfigFlowResult
from homeassistant.helpers.selector import (
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
)

from .client import (
    TIMEZONE,
    AquastarError,
    AuthenticationError,
    CannotConnectError,
    download_usage,
)
from .const import (
    CONF_BILLING_DAY,
    CONF_METER_NUMBER,
    CONF_SECTOKEN,
    DEFAULT_BILLING_DAY,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)

STEP_USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_SECTOKEN): str,
    }
)


class AquastarOptionsFlow(config_entries.OptionsFlow):
    """Handle options for Aquastar."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(data=user_input)

        current_day = self.config_entry.options.get(
            CONF_BILLING_DAY, DEFAULT_BILLING_DAY
        )
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_BILLING_DAY, default=current_day): NumberSelector(
                        NumberSelectorConfig(min=1, max=28, mode=NumberSelectorMode.BOX)
                    ),
                }
            ),
        )


class AquastarConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Aquastar."""

    VERSION = 1

    @staticmethod
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> AquastarOptionsFlow:
        return AquastarOptionsFlow()

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle sectoken configuration."""
        errors: dict[str, str] = {}

        if user_input is not None:
            sectoken = user_input[CONF_SECTOKEN].strip()
            meters, error = await self._async_validate_sectoken(sectoken)

            if error:
                errors["base"] = error
            elif len(meters) == 1:
                meter_number = meters[0]
                await self.async_set_unique_id(meter_number)
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=f"Aquastar ({meter_number})",
                    data={
                        CONF_SECTOKEN: sectoken,
                        CONF_METER_NUMBER: meter_number,
                    },
                )
            else:
                self._sectoken = sectoken
                self._available_meters = meters
                return await self.async_step_select_meter()

        return self.async_show_form(
            step_id="user",
            data_schema=STEP_USER_SCHEMA,
            errors=errors,
        )

    async def async_step_select_meter(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle meter selection when multiple meters exist on the account."""
        errors: dict[str, str] = {}

        if user_input is not None:
            meter_number = user_input[CONF_METER_NUMBER]
            await self.async_set_unique_id(meter_number)
            self._abort_if_unique_id_configured()
            return self.async_create_entry(
                title=f"Aquastar ({meter_number})",
                data={
                    CONF_SECTOKEN: self._sectoken,
                    CONF_METER_NUMBER: meter_number,
                },
            )

        return self.async_show_form(
            step_id="select_meter",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_METER_NUMBER): SelectSelector(
                        SelectSelectorConfig(
                            options=self._available_meters,
                            mode=SelectSelectorMode.LIST,
                        )
                    )
                }
            ),
            errors=errors,
        )

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        """Handle re-authentication when the sectoken becomes invalid."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle user input for re-authentication."""
        errors: dict[str, str] = {}

        if user_input is not None:
            sectoken = user_input[CONF_SECTOKEN].strip()
            reauth_entry = self._get_reauth_entry()
            meters, error = await self._async_validate_sectoken(sectoken)

            if error:
                errors["base"] = error
            elif reauth_entry.data[CONF_METER_NUMBER] not in meters:
                errors["base"] = "meter_mismatch"
            else:
                return self.async_update_reload_and_abort(
                    reauth_entry,
                    data={
                        **reauth_entry.data,
                        CONF_SECTOKEN: sectoken,
                    },
                )

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=STEP_USER_SCHEMA,
            errors=errors,
        )

    async def _async_validate_sectoken(
        self, sectoken: str
    ) -> tuple[list[str], None] | tuple[None, str]:
        """Validate the sectoken by fetching recent data.

        Returns (meters, None) on success, where meters is an ordered list of
        distinct meter numbers found in the results, or (None, error_key) on
        failure.
        """
        try:
            end = datetime.now(ZoneInfo(TIMEZONE)).date()
            start = end - timedelta(days=7)
            readings = await download_usage(sectoken, start, end)

            if not readings:
                _LOGGER.error("No readings returned during validation")
                return None, "no_readings"

            seen: set[str] = set()
            meters: list[str] = []
            for r in readings:
                if r.meter_number not in seen:
                    seen.add(r.meter_number)
                    meters.append(r.meter_number)

            return meters, None

        except AuthenticationError:
            _LOGGER.error("Invalid sectoken")
            return None, "invalid_auth"
        except CannotConnectError:
            _LOGGER.error("Cannot connect to Aquastar portal")
            return None, "cannot_connect"
        except AquastarError:
            _LOGGER.exception("Unexpected Aquastar error during validation")
            return None, "unknown"
