"""#49: the failure labeler never sends a local-only repo's trace to a cloud model, even
when a cloud labeler is explicitly allowed. Reuses ``test_failure_label``'s fixtures."""

from __future__ import annotations

import test_failure_label as tfl
from foreman_inator.tracing.failure_label import label_kickback

# test_failure_label's fixtures, used here by name.
store, rec_dir, fake, proxy = tfl.store, tfl.rec_dir, tfl.fake, tfl.proxy
CONFIG, _setup, chats, oracle = tfl.CONFIG, tfl._setup, tfl.chats, tfl.oracle


def test_allowed_cloud_labeler_is_still_refused_for_a_local_only_repo(store, rec_dir, fake, proxy):
    fake.responder = oracle
    t, _, k, _ = _setup(store, rec_dir, "wrong_edit")
    store.set_cloud(t.repo, False)
    glm = CONFIG.models_for_tier(3)[0]
    assert glm.is_cloud
    res = label_kickback(
        store, k.id, recordings_dir=rec_dir, config=CONFIG, proxy=proxy, labeler=glm,
        allow_cloud=True,
    )  # fmt: skip
    assert res.label is None and "local only" in res.reason
    assert fake.requests == [] and chats(fake) == []
    assert store.get_kickback(k.id).failure_step is None


def test_local_labeler_still_labels_a_local_only_repo(store, rec_dir, fake, proxy):
    fake.responder = oracle
    t, _, k, _ = _setup(store, rec_dir, "wrong_edit")
    store.set_cloud(t.repo, False)
    res = label_kickback(store, k.id, recordings_dir=rec_dir, config=CONFIG, proxy=proxy)
    assert res.label == "wrong_edit" and res.source == "model", res.reason
