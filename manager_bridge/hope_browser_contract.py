"""Browser plan only: never installs/starts/falls back to an unsupported browser."""
import os
import re
import shutil

SITES_INSTRUCTION='skill://plugin_connector_1p_689987207de08191979cf68eca2941c6/sites/SKILL.md'


def browser_plan(source):
    if not re.fullmatch('[a-f0-9]{40}',source):raise ValueError('exact browser candidate required')
    present=shutil.which('cua_repl.js') is not None
    return dict(schema='hope-supported-browser-plan/v1',source_revision=source,
        environment='managed-linux' if os.environ.get('SITES_MANAGED_LINUX_CONTAINER')=='1' else 'unsupported-host-profile',
        supported_tool_present=present,status='PREPARATION_REQUIRED' if present else 'NOT RUN',
        instruction=SITES_INSTRUCTION,reason=None if present else 'cua_repl_unavailable_skip_browser_qa',
        automatic_fallback=False,preview_started=False,browser_installed=False,actual_browser_verified=False,
        widths=[1280,390,360],routes=['/review/','/review/features','/review/app','/review/policy'],
        required_preconditions=['approved supported preview reference','candidate SHA before/after match','authenticated isolated seeded workspace','API/journal initial state match'],
        required_actions=['keyboard focus summary','Enter/Space open and close','touch open and close','screen/API/journal balances compare','horizontal overflow measurement','source-bound screenshots'],
        excluded_claims=['actual device verified by viewport','actual PG','production deployment'])
