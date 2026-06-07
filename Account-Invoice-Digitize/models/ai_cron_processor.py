import json
import logging
from datetime import timedelta

from odoo import api, fields, models

_logger = logging.getLogger(__name__)

_CRON_BATCH_SIZE = 5
_CRON_STALE_MINUTES = 10


class AccountMove(models.Model):
    _inherit = 'account.move'

    # ===================================================================
    # Cron-based background extraction
    # ===================================================================

    @api.model
    def _ai_cron_process_queue(self):
        """Cron job: process queued AI extractions (background mode)."""
        # Mark stale items (queued > _CRON_STALE_MINUTES ago) as failed
        cutoff = fields.Datetime.now() - timedelta(minutes=_CRON_STALE_MINUTES)
        stale = self.search([
            ('ai_extraction_status', '=', 'processing'),
            ('ai_extraction_queued_at', '!=', False),
            ('ai_extraction_queued_at', '<', cutoff),
        ])
        if stale:
            _logger.warning('AI cron: marking %d stale extractions as failed', len(stale))
            stale.write({'ai_extraction_status': 'failed', 'ai_extraction_queued_at': False})

        moves = self.search(
            [
                ('ai_extraction_status', '=', 'processing'),
                ('ai_extraction_queued_at', '!=', False),
            ],
            order='ai_extraction_queued_at asc',
            limit=_CRON_BATCH_SIZE,
        )
        if not moves:
            return

        ICP = self.env['ir.config_parameter'].sudo()
        api_key = ICP.get_param('account_invoice_digitize_ai.ai_api_key')
        if not api_key:
            _logger.warning('AI cron: no API key configured, skipping queue')
            return

        for move in moves:
            try:
                self._ai_cron_extract_one(move, api_key)
            except Exception:
                _logger.exception('AI cron: extraction failed for move %s', move.id)
                move.ai_extraction_status = 'failed'
                move.ai_extraction_queued_at = False
            # Commit after each invoice to avoid losing work on error
            self.env.cr.commit()  # noqa: B010

    def _ai_cron_extract_one(self, move, api_key):
        """Process a single queued extraction."""
        attachment = move._ai_get_invoice_attachment()
        if not attachment:
            move.ai_extraction_status = 'failed'
            move.ai_extraction_queued_at = False
            return

        data = move._ai_trigger_extraction(api_key, attachment, preview=True)
        if data is None:
            # Only mark failed if trigger_extraction didn't already set a final status
            # (e.g. proforma documents set 'done' internally before returning None)
            if move.ai_extraction_status == 'processing':
                move.ai_extraction_status = 'failed'
        else:
            move.ai_last_extraction_data = json.dumps(data)
            move.ai_last_extraction_attachment_id = attachment.id
            # Auto-apply if conditions are met
            if move._ai_can_auto_apply(data):
                try:
                    move._ai_apply_extraction(data)
                except Exception:
                    _logger.warning(
                        'AI cron: auto-apply failed for move %s',
                        move.id, exc_info=True,
                    )
            move.ai_extraction_status = 'done'
        move.ai_extraction_queued_at = False
