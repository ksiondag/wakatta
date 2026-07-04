"""One-off fixup: recompute `order_index` for every page's sentences using the
current reading_order.py heuristic. Safe to re-run any time — e.g. after tuning
the heuristic, or for pages ingested before this sort existed (though those are
also backfilled automatically on next server startup, see server.py's lifespan).
"""
from sqlalchemy.orm import Session

from server import Page, _ensure_order_index_column, _resort_page, engine


def main() -> None:
    _ensure_order_index_column()
    with Session(engine) as session:
        page_ids = [p.id for p in session.query(Page.id).all()]
        for page_id in page_ids:
            _resort_page(session, page_id)
        session.commit()
    print(f"Re-sorted {len(page_ids)} pages.")


if __name__ == "__main__":
    main()
