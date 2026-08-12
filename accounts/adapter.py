from allauth.account.adapter import DefaultAccountAdapter

# The two login stages that exist only to make a passkey a *second* factor.
# Named as strings because that is how allauth lists them.
SECOND_FACTOR_STAGES = {
    "allauth.mfa.stages.AuthenticateStage",
    "allauth.mfa.stages.TrustStage",
}


class AccountAdapter(DefaultAccountAdapter):
    """A passkey is a way in, never a gate in front of the password.

    allauth's mfa app is installed here for one reason: it implements WebAuthn.
    But its login stage does not distinguish "this account has a passkey" from
    "this account has opted into two-factor authentication" — the moment any
    WebAuthn authenticator exists, every password sign-in is interrupted and
    made to produce the key as well. Nobody asked for that by registering Touch
    ID, and the account it protects can post a church bazaar to a listings
    page. Worse, it is a way to be locked out by a feature that was sold as
    being *less* work: a passkey lives in one laptop's secure enclave, and with
    recovery codes deliberately out (see MFA_SUPPORTED_TYPES in settings) a
    borrowed phone has no second step to offer.

    So the stage comes out. Passkey login itself is untouched — it is not a
    stage, it is an alternative to the password on the sign-in page — and the
    email-verification stage above it stays exactly as it was.
    """

    def get_login_stages(self) -> list[str]:
        return [s for s in super().get_login_stages() if s not in SECOND_FACTOR_STAGES]
