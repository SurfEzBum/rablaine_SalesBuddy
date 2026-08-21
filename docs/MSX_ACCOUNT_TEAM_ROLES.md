# MSX Native Account V-Team

How to retrieve seller and solution engineer assignments from the native MSX
account access team.

**Last verified:** August 20, 2026
**API:** `https://microsoftsales.crm.dynamics.com/api/data/v9.2`
**Account access team template:** `3fcc1cfc-3e43-e311-9405-00155db3ba1e`

## Decision

Use the native Dynamics account access team for both account discovery and
seller/solution engineer enrichment. Do not derive these assignments from
`msp_accountteams`, territory ownership, account ownership, or territory-level
majority voting.

The native v-team returned the correct seller and Acquisition/Growth type for
49 of 50 validated accounts. The only miss was a stale local assignment to a
disabled user who was absent from both current MSX sources. It also returned
the expected Data, Infrastructure, and Apps solution engineers.

This investigation covers only data Sales Buddy already synchronizes:

- Growth seller
- Acquisition seller
- Data solution engineer
- Infrastructure solution engineer
- Apps solution engineer

## Native Data Model

Each account has an access `team` created from the Account Access Team
template. Team membership links that team to current `systemuser` records:

```text
account
  -> team (teamtype = 1, Account Access Team template)
    -> teammembership
      -> systemuser
```

Relevant entities and fields:

| Entity | Fields | Purpose |
|---|---|---|
| `team` | `teamid`, `regardingobjectid`, `teamtype`, `teamtemplateid` | Identifies one account's native access team |
| `teammembership` | `teamid`, `systemuserid` | Joins team members to users |
| `systemuser` | `systemuserid`, `fullname`, `internalemailaddress`, `domainname`, `title`, `msp_qualifier1`, `msp_qualifier2`, `isdisabled` | Supplies identity and role metadata |

`regardingobjectid` is the account GUID. Team names also use
`{account_guid}+{team_template_guid}`, but name parsing should remain a fallback.

## Account Discovery

Discover all accounts assigned to the current user through their native team
memberships:

```http
GET /systemusers({systemuser_id})/teammembership_association
    ?$select=_regardingobjectid_value,teamid,name
    &$filter=teamtype eq 1
             and _teamtemplateid_value eq 3fcc1cfc-3e43-e311-9405-00155db3ba1e
    &$top=5000
```

Follow `@odata.nextLink`, collect distinct `_regardingobjectid_value` values,
then batch-query `accounts` for TPID, name, and territory metadata.

Sales Buddy already uses this path in
`app/services/msx_api.py::get_my_account_team_ids()`.

## Retrieve One Account's V-Team

Use FetchXML to resolve the account's access team and join directly to its
members. This avoids downloading hundreds of members and filtering locally.

```xml
<fetch distinct="true">
  <entity name="team">
    <attribute name="teamid" />
    <attribute name="regardingobjectid" />
    <filter>
      <condition attribute="teamtype" operator="eq" value="1" />
      <condition attribute="teamtemplateid" operator="eq"
                 value="3fcc1cfc-3e43-e311-9405-00155db3ba1e" />
      <condition attribute="regardingobjectid" operator="eq"
                 value="{account_guid}" />
    </filter>
    <link-entity name="teammembership"
                 from="teamid"
                 to="teamid"
                 intersect="true">
      <link-entity name="systemuser"
                   from="systemuserid"
                   to="systemuserid"
                   alias="member">
        <attribute name="systemuserid" />
        <attribute name="fullname" />
        <attribute name="internalemailaddress" />
        <attribute name="domainname" />
        <attribute name="title" />
        <attribute name="msp_qualifier1" />
        <attribute name="msp_qualifier2" />
        <attribute name="isdisabled" />
        <!-- Add the role-specific filter here. -->
      </link-entity>
    </link-entity>
  </entity>
</fetch>
```

Send the encoded query through the standard Dataverse endpoint:

```http
GET /teams?fetchXml={encoded_fetchxml}
```

## Seller Identification

Apply all seller conditions to the linked `systemuser`:

| Field | Required value |
|---|---|
| `isdisabled` | `false` |
| `msp_qualifier1` | `Corporate` |
| `msp_qualifier2` | `Cloud & AI` or `Cloud & AI-Acq` |
| `title` | Contains `Digital Specialist` |
| `title` | Does not contain `Dir` or `Manager` |

Map seller type from `msp_qualifier2`:

| `msp_qualifier2` | Seller type |
|---|---|
| `Cloud & AI` | Growth |
| `Cloud & AI-Acq` | Acquisition |

Title exclusions matter. Broad Cloud & AI filtering includes directors,
managers, and overlay specialists. With the complete filter, every one of the
49 accounts that returned a current seller had exactly one candidate.

Extract the Sales Buddy alias from `internalemailaddress`, falling back to
`domainname`:

```python
email = member.get("internalemailaddress") or member.get("domainname") or ""
alias = email.split("@", 1)[0] if "@" in email else email
```

## Solution Engineer Identification

Apply these common conditions:

| Field | Required value |
|---|---|
| `isdisabled` | `false` |
| `msp_qualifier1` | `Corporate` |
| `title` | Contains `Sol Engineer` |

Then map the person's `msp_qualifier2`:

| `msp_qualifier2` | Sales Buddy field |
|---|---|
| `Cloud & AI Data` | Data SE |
| `Cloud & AI Infrastructure` | Infrastructure SE |
| `Cloud & AI Apps` | Apps SE |

Verified examples:

| Person | `msp_qualifier2` | Classification |
|---|---|---|
| Alex Blaine | `Cloud & AI Data` | Data SE |
| Ben Magazino | `Cloud & AI Infrastructure` | Infrastructure SE |
| Harry Arce | `Cloud & AI Apps` | Apps SE |

The linked `systemuser` values are important. Role fields on
`msp_accountteams` were retaxonomized and no longer expose the old Cloud & AI
specialties consistently, while current person records retain the values
needed by Sales Buddy.

## Validation

Seller validation used 50 deterministic, stratified accounts selected from the
current user's 210 native account memberships. The sample covered all four
territories represented in the book and both known seller types.

| Result | Count |
|---|---:|
| Exact seller and type match | 49 |
| Current native seller missing | 1 |
| Wrong seller returned | 0 |
| Multiple candidates after complete filtering | 0 |

The sole miss was Lendmark Financial Services. Its stored seller, Britt
Matthews, is disabled in `systemusers` and absent from the native v-team. This
indicates stale local data rather than a false native-team result.

The native source also fixed known omissions from `msp_accountteams`, including
Dan Kraft on 3 Arrows Services.

## Why `msp_accountteams` Is Retired

`msp_accountteams` is not reliable for current account enrichment:

- It can omit current sellers who are present on the native v-team.
- It can retain overlay or stale rows that resemble territory sellers.
- Its row-level qualifier taxonomy changed and no longer maps cleanly to the
  seller and SE fields Sales Buddy stores.
- Queries against the large entity are slow and can trigger misleading VPN
  failure handling after a timeout.
- Territory-level inference built on these rows produced incorrect sellers.

Account owner and territory owner are not substitutes. Account owner is an
account executive; territory owner is commonly a manager.

## Account Sync Rewrite Contract

The next account-sync rewrite should:

1. Keep `get_my_account_team_ids()` for current-user account discovery.
2. Batch-query account details as it does today.
3. Query each account's native v-team through the access-team FetchXML join.
4. Apply seller and SE filters to linked `systemuser` records server-side.
5. Map seller type and SE specialty from `systemuser.msp_qualifier2`.
6. Resolve aliases from user email fields.
7. Exclude disabled users, directors, and managers.
8. Preserve an existing local assignment when no valid current member is
   returned; do not replace it with a guessed account or territory owner.
9. Remove `msp_accountteams` enrichment, overlay suppression, territory
   majority voting, and related fallback logic.
10. Add focused tests for Growth, Acquisition, all three SE specialties,
    disabled users, management exclusions, no result, and duplicate members.

## Query Guidance

- Use the exact Account Access Team template ID; users also belong to milestone,
  opportunity, and account-plan access teams.
- Filter linked users server-side so large v-teams do not inflate responses.
- Treat titles case-insensitively.
- Follow `@odata.nextLink` whenever present.
- Include `Prefer: odata.include-annotations="*"` only when formatted lookup
  labels are needed.
- Authenticate for
  `https://microsoftsales.crm.dynamics.com` in tenant
  `72f988bf-86f1-41af-91ab-2d7cd011db47`.
