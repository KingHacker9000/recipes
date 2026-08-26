from django.core.management.base import BaseCommand, CommandError

from cookbook.helper.ai_runtime import codex_connected, provider_runtime_status, start_codex_device_login


class Command(BaseCommand):
    help = "Sign Tandoor's isolated Codex runtime into ChatGPT using a device code."

    def handle(self, *args, **options):
        if codex_connected():
            self.stdout.write(self.style.SUCCESS("Codex already has a ChatGPT credential."))
            return

        state = start_codex_device_login()
        if state.get("error"):
            raise CommandError(state["error"])

        self.stdout.write("Open this URL in a trusted browser:")
        self.stdout.write(str(state.get("verification_url") or "(waiting for device URL)"))
        if state.get("user_code"):
            self.stdout.write(f"Code: {state['user_code']}")
        self.stdout.write("The login worker will finish in the background after authorization.")
