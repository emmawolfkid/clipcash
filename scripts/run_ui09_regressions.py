"""UI-09 regressions, or --full for the entire guarded PostgreSQL suite."""
import sys

import run_ui02_regressions as guarded

if '--full' in sys.argv:
    sys.argv.remove('--full')
    guarded.TEST_LABELS = ()
else:
    guarded.TEST_LABELS = (
        'apps.core.tests.test_ui09',
        'apps.core.tests.test_design_system',
        'apps.core.tests.test_public_auth_ui',
        'apps.core.tests.test_member_viewing_ui',
        'apps.core.tests.test_frontend_acceptance',
        'apps.accounts.tests.test_reg02_phone_first',
        'apps.accounts.tests.test_registration',
        'apps.payments.tests.test_manual_web',
        'apps.payments.tests.test_manual',
        'apps.payments.tests.test_pay03_acceptance',
        'apps.rewards.tests.test_credits',
        'apps.notifications.tests.test_inbox',
        'apps.memberships.tests.test_complimentary',
        'apps.core.tests.test_deployment',
        'apps.core.tests.test_deploy_safety',
    )

if __name__ == '__main__':
    guarded.main(suite_name='UI-09')
