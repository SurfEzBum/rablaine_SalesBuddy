# FY Archive Explorer

## Overview

Add a read-only archive browser to the Fiscal Year Management section of the Admin Panel. When a user clicks "Browse" on a previous year's archive, a modal opens with a two-panel layout: a searchable navigation tree on the left (organized by Seller → Customer → data) and a detail panel on the right that shows context for whatever node is selected.

The archive `.db` file is opened read-only via a temporary SQLAlchemy connection — no risk to the live database.

---

## Design

### Modal Layout

```
┌───────────────────────────────────────────────────────────┐
│  📦 FY25 Archive  (Dec 15, 2025 · 24.3 MB)          [X]  │
├────────────────────────┬──────────────────────────────────┤
│ 🔍 [Search...        ] │                                  │
│                        │      (Detail Panel)              │
│ 📊 Summary             │                                  │
│ 👤 Jane Smith (18)     │  Adapts to whatever is selected  │
│   ▼ 👥 Acme Corp      │  in the tree. Every node is      │
│     ▸ 📝 Notes (12)   │  clickable. Links in the detail  │
│     ▸ 🤝 Engmnts (3)  │  panel also drive tree           │
│     ▸ 🏔️ Milestns (5) │  navigation.                     │
│   ▸ 👥 Contoso Ltd    │                                  │
│ ▸ 👤 Bob Jones (7)    │                                  │
│ ▸ 📋 Unassigned (3)   │                                  │
└────────────────────────┴──────────────────────────────────┘
```

### Navigation Tree Structure

Single drill-down path: **Seller → Customer → Category → Individual Item**

```
📊 Summary
👤 Seller Name (N customers)
├── 👥 Customer Name (TPID)
│   ├── 📝 Notes (count)
│   │   ├── Note title — date
│   │   └── Note title — date
│   ├── 🤝 Engagements (count)
│   │   ├── Engagement name
│   │   └── Engagement name
│   └── 🏔️ Milestones (count)
│       ├── Milestone name
│       └── Milestone name
```

An **"Unassigned"** pseudo-seller node appears at the bottom for customers with no seller (manually created, no MSX data).

### Every Node Is Clickable

| Click on... | Tree behavior | Detail panel shows |
|---|---|---|
| **Summary** | No children | Archive stats: date, size, total sellers/customers/notes/engagements/milestones |
| **Seller** | Expands to show customers | Seller card: name, alias, **clickable customer list** |
| **Customer** | Expands to show Notes/Engagements/Milestones category nodes | Customer card: name, TPID, territory, verticals, seller, counts |
| **"Notes (12)"** | Expands to list individual notes | Customer's notes as a **clickable list** with dates and previews |
| **"Engagements (3)"** | Expands to list individual engagements | Customer's engagements as a **clickable list** |
| **"Milestones (5)"** | Expands to list individual milestones | Customer's milestones as a **clickable list** |
| **Individual Note** | Leaf node, no children | Full note: date, attendees, topics, partners, content |
| **Individual Engagement** | Leaf node, no children | Engagement detail: name, status, linked notes |
| **Individual Milestone** | Leaf node, no children | Milestone detail: name, status, tasks, linked notes |

### Detail Panel Links Drive Tree Navigation

When the detail panel shows clickable items (e.g., a seller's customer list), clicking one:
1. Expands that node in the tree (if not already)
2. Selects it in the tree
3. Updates the detail panel to show that item

This gives two navigation patterns:
- **"I'm browsing"** → expand nodes in the tree, click leaves
- **"I found what I want"** → click links in the detail panel to jump deeper

### Search Behavior

A search field at the top of the tree panel provides instant client-side filtering on the already-loaded tree data (sellers + customers are a small dataset).

| User types... | Result |
|---|---|
| Customer name (e.g., "acme") | Auto-expands the parent seller, highlights matching customers |
| Engagement name | Expands seller → customer → highlights the engagement |
| Seller name | Highlights the seller node |
| Clear search | Collapses back to default state |

Search data (sellers, customers, engagement names, note titles) is loaded once when the modal opens as part of the tree endpoint response. No additional API calls needed for filtering.

---

## Implementation

### Backend

#### 1. Archive Connection Manager — `app/services/fy_cutover.py`

Add a context manager that opens an archive `.db` read-only:

```python
import sqlite3
from contextlib import contextmanager
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

@contextmanager
def open_archive(fy_label: str):
    """Open an FY archive database read-only.

    Usage:
        with open_archive("FY25") as session:
            customers = session.execute(text("SELECT * FROM customer")).fetchall()

    The connection uses SQLite URI mode with ?mode=ro to prevent any writes.
    """
    data_dir = _get_data_dir()
    archive_path = data_dir / f"{fy_label}.db"

    if not archive_path.exists():
        raise FileNotFoundError(f"Archive not found: {archive_path}")

    # file: URI with mode=ro ensures read-only access
    uri = f"sqlite:///file:{archive_path}?mode=ro&uri=true"
    engine = create_engine(uri, echo=False)
    session = Session(engine)
    try:
        yield session
    finally:
        session.close()
        engine.dispose()
```

Add archive query helper functions:

```python
def get_archive_summary(fy_label: str) -> dict:
    """Get high-level stats from an archive."""
    with open_archive(fy_label) as session:
        return {
            "fy_label": fy_label,
            "customers": session.execute(text("SELECT COUNT(*) FROM customer")).scalar(),
            "notes": session.execute(text("SELECT COUNT(*) FROM note")).scalar(),
            "engagements": session.execute(text("SELECT COUNT(*) FROM engagement")).scalar(),
            "milestones": session.execute(text("SELECT COUNT(*) FROM milestone")).scalar(),
            "sellers": session.execute(text("SELECT COUNT(*) FROM seller")).scalar(),
            "territories": session.execute(text("SELECT COUNT(*) FROM territory")).scalar(),
            "opportunities": session.execute(text("SELECT COUNT(*) FROM opportunity")).scalar(),
        }


def get_archive_tree(fy_label: str) -> dict:
    """Get the full navigation tree: sellers with customers and counts.

    Also returns searchable metadata (engagement names, note titles)
    so the frontend can do instant client-side filtering.
    """
    with open_archive(fy_label) as session:
        # Get all sellers
        sellers_raw = session.execute(text("""
            SELECT s.id, s.name, s.alias,
                   COUNT(DISTINCT c.id) as customer_count
            FROM seller s
            LEFT JOIN customer c ON c.seller_id = s.id
            GROUP BY s.id
            ORDER BY s.name
        """)).fetchall()

        # Get all customers with their seller assignment
        customers_raw = session.execute(text("""
            SELECT c.id, c.name, c.tpid, c.seller_id,
                   t.name as territory_name,
                   (SELECT COUNT(*) FROM note WHERE customer_id = c.id) as note_count,
                   (SELECT COUNT(*) FROM engagement WHERE customer_id = c.id) as engagement_count,
                   (SELECT COUNT(*) FROM milestone WHERE customer_id = c.id) as milestone_count
            FROM customer c
            LEFT JOIN territory t ON c.territory_id = t.id
            ORDER BY c.name
        """)).fetchall()

        # Build searchable index: engagement names and note titles per customer
        search_data = {}
        for cust in customers_raw:
            cid = cust.id
            notes = session.execute(text("""
                SELECT id, title, call_date FROM note
                WHERE customer_id = :cid ORDER BY call_date DESC
            """), {"cid": cid}).fetchall()

            engagements = session.execute(text("""
                SELECT id, name FROM engagement
                WHERE customer_id = :cid ORDER BY name
            """), {"cid": cid}).fetchall()

            milestones = session.execute(text("""
                SELECT id, name FROM milestone
                WHERE customer_id = :cid ORDER BY name
            """), {"cid": cid}).fetchall()

            search_data[cid] = {
                "notes": [{"id": n.id, "title": n.title, "date": str(n.call_date)} for n in notes],
                "engagements": [{"id": e.id, "name": e.name} for e in engagements],
                "milestones": [{"id": m.id, "name": m.name} for m in milestones],
            }

        # Group customers by seller
        seller_customers = {}
        unassigned = []
        for c in customers_raw:
            entry = {
                "id": c.id,
                "name": c.name,
                "tpid": c.tpid,
                "territory": c.territory_name,
                "note_count": c.note_count,
                "engagement_count": c.engagement_count,
                "milestone_count": c.milestone_count,
                "search": search_data.get(c.id, {}),
            }
            if c.seller_id:
                seller_customers.setdefault(c.seller_id, []).append(entry)
            else:
                unassigned.append(entry)

        sellers = []
        for s in sellers_raw:
            sellers.append({
                "id": s.id,
                "name": s.name,
                "alias": s.alias,
                "customer_count": s.customer_count,
                "customers": seller_customers.get(s.id, []),
            })

        # Add unassigned pseudo-seller if any
        if unassigned:
            sellers.append({
                "id": None,
                "name": "Unassigned",
                "alias": None,
                "customer_count": len(unassigned),
                "customers": unassigned,
            })

        return {
            "fy_label": fy_label,
            "sellers": sellers,
            "summary": get_archive_summary(fy_label),
        }


def get_archive_customer(fy_label: str, customer_id: int) -> dict:
    """Get full customer data from an archive (notes, engagements, milestones)."""
    with open_archive(fy_label) as session:
        customer = session.execute(text("""
            SELECT c.*, s.name as seller_name, s.alias as seller_alias,
                   t.name as territory_name
            FROM customer c
            LEFT JOIN seller s ON c.seller_id = s.id
            LEFT JOIN territory t ON c.territory_id = t.id
            WHERE c.id = :cid
        """), {"cid": customer_id}).fetchone()

        if not customer:
            return None

        # Get verticals
        verticals = session.execute(text("""
            SELECT v.name FROM vertical v
            JOIN customer_verticals cv ON cv.vertical_id = v.id
            WHERE cv.customer_id = :cid
        """), {"cid": customer_id}).fetchall()

        # Get notes with topics
        notes = session.execute(text("""
            SELECT n.id, n.title, n.call_date, n.body, n.attendees
            FROM note n WHERE n.customer_id = :cid
            ORDER BY n.call_date DESC
        """), {"cid": customer_id}).fetchall()

        notes_list = []
        for n in notes:
            topics = session.execute(text("""
                SELECT t.name FROM topic t
                JOIN note_topics nt ON nt.topic_id = t.id
                WHERE nt.note_id = :nid
            """), {"nid": n.id}).fetchall()
            notes_list.append({
                "id": n.id,
                "title": n.title,
                "call_date": str(n.call_date) if n.call_date else None,
                "body": n.body,
                "attendees": n.attendees,
                "topics": [t.name for t in topics],
            })

        # Get engagements with linked notes
        engagements = session.execute(text("""
            SELECT e.id, e.name, e.status
            FROM engagement e WHERE e.customer_id = :cid
            ORDER BY e.name
        """), {"cid": customer_id}).fetchall()

        engagements_list = []
        for e in engagements:
            eng_notes = session.execute(text("""
                SELECT n.id, n.title, n.call_date
                FROM note n WHERE n.engagement_id = :eid
                ORDER BY n.call_date DESC
            """), {"eid": e.id}).fetchall()
            engagements_list.append({
                "id": e.id,
                "name": e.name,
                "status": e.status,
                "notes": [{"id": en.id, "title": en.title, "date": str(en.call_date)} for en in eng_notes],
            })

        # Get milestones with tasks
        milestones = session.execute(text("""
            SELECT m.id, m.name, m.status
            FROM milestone m WHERE m.customer_id = :cid
            ORDER BY m.name
        """), {"cid": customer_id}).fetchall()

        milestones_list = []
        for m in milestones:
            tasks = session.execute(text("""
                SELECT t.id, t.name, t.status
                FROM msx_task t WHERE t.milestone_id = :mid
                ORDER BY t.name
            """), {"mid": m.id}).fetchall()
            linked_notes = session.execute(text("""
                SELECT n.id, n.title, n.call_date
                FROM note n
                JOIN notes_milestones nm ON nm.note_id = n.id
                WHERE nm.milestone_id = :mid
                ORDER BY n.call_date DESC
            """), {"mid": m.id}).fetchall()
            milestones_list.append({
                "id": m.id,
                "name": m.name,
                "status": m.status,
                "tasks": [{"id": t.id, "name": t.name, "status": t.status} for t in tasks],
                "linked_notes": [{"id": ln.id, "title": ln.title, "date": str(ln.call_date)} for ln in linked_notes],
            })

        return {
            "id": customer.id,
            "name": customer.name,
            "tpid": customer.tpid,
            "seller_name": customer.seller_name,
            "seller_alias": customer.seller_alias,
            "territory": customer.territory_name,
            "verticals": [v.name for v in verticals],
            "notes": notes_list,
            "engagements": engagements_list,
            "milestones": milestones_list,
        }


def get_archive_detail(fy_label: str, item_type: str, item_id: int) -> dict:
    """Get a single note, engagement, or milestone detail from an archive."""
    with open_archive(fy_label) as session:
        if item_type == "note":
            note = session.execute(text("""
                SELECT n.*, c.name as customer_name
                FROM note n
                LEFT JOIN customer c ON n.customer_id = c.id
                WHERE n.id = :nid
            """), {"nid": item_id}).fetchone()
            if not note:
                return None

            topics = session.execute(text("""
                SELECT t.name FROM topic t
                JOIN note_topics nt ON nt.topic_id = t.id
                WHERE nt.note_id = :nid
            """), {"nid": item_id}).fetchall()

            partners = session.execute(text("""
                SELECT p.name FROM partner p
                JOIN note_partners np ON np.partner_id = p.id
                WHERE np.note_id = :nid
            """), {"nid": item_id}).fetchall()

            milestone_links = session.execute(text("""
                SELECT m.id, m.name FROM milestone m
                JOIN notes_milestones nm ON nm.milestone_id = m.id
                WHERE nm.note_id = :nid
            """), {"nid": item_id}).fetchall()

            return {
                "type": "note",
                "id": note.id,
                "title": note.title,
                "call_date": str(note.call_date) if note.call_date else None,
                "body": note.body,
                "attendees": note.attendees,
                "customer_name": note.customer_name,
                "customer_id": note.customer_id,
                "topics": [t.name for t in topics],
                "partners": [p.name for p in partners],
                "milestones": [{"id": m.id, "name": m.name} for m in milestone_links],
            }

        elif item_type == "engagement":
            eng = session.execute(text("""
                SELECT e.*, c.name as customer_name
                FROM engagement e
                LEFT JOIN customer c ON e.customer_id = c.id
                WHERE e.id = :eid
            """), {"eid": item_id}).fetchone()
            if not eng:
                return None

            eng_notes = session.execute(text("""
                SELECT n.id, n.title, n.call_date
                FROM note n WHERE n.engagement_id = :eid
                ORDER BY n.call_date DESC
            """), {"eid": item_id}).fetchall()

            return {
                "type": "engagement",
                "id": eng.id,
                "name": eng.name,
                "status": eng.status,
                "customer_name": eng.customer_name,
                "customer_id": eng.customer_id,
                "notes": [{"id": n.id, "title": n.title, "date": str(n.call_date)} for n in eng_notes],
            }

        elif item_type == "milestone":
            ms = session.execute(text("""
                SELECT m.*, c.name as customer_name
                FROM milestone m
                LEFT JOIN customer c ON m.customer_id = c.id
                WHERE m.id = :mid
            """), {"mid": item_id}).fetchone()
            if not ms:
                return None

            tasks = session.execute(text("""
                SELECT t.id, t.name, t.status
                FROM msx_task t WHERE t.milestone_id = :mid
                ORDER BY t.name
            """), {"mid": item_id}).fetchall()
            linked_notes = session.execute(text("""
                SELECT n.id, n.title, n.call_date
                FROM note n
                JOIN notes_milestones nm ON nm.note_id = n.id
                WHERE nm.milestone_id = :mid
                ORDER BY n.call_date DESC
            """), {"mid": item_id}).fetchall()

            return {
                "type": "milestone",
                "id": ms.id,
                "name": ms.name,
                "status": ms.status,
                "customer_name": ms.customer_name,
                "customer_id": ms.customer_id,
                "tasks": [{"id": t.id, "name": t.name, "status": t.status} for t in tasks],
                "linked_notes": [{"id": ln.id, "title": ln.title, "date": str(ln.call_date)} for ln in linked_notes],
            }

        return None
```