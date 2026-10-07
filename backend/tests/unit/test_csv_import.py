"""CSV import parsing.

The import used to be a stub that returned ``dry_run_completed`` and wrote
nothing. These cover the parsing and the per-row validation the PRD requires
("errors are shown before import commit"), including the edge cases it names.
"""

from app.application.events.csv_import import MemberCsvParser, TeamCsvParser
from app.domain.common.enums import Role


class TestMemberCsv:
    def test_parses_email_and_role(self):
        rows, errors = MemberCsvParser().parse(
            "email,role,name\na@x.com,participant,Ann\nb@x.com,mentor,Bob\n"
        )
        assert not errors
        assert [row.email for row in rows] == ["a@x.com", "b@x.com"]
        assert [row.role for row in rows] == [Role.PARTICIPANT, Role.MENTOR]
        assert rows[0].display_name == "Ann"

    def test_role_defaults_to_participant(self):
        rows, errors = MemberCsvParser().parse("email\na@x.com\n")
        assert not errors
        assert rows[0].role is Role.PARTICIPANT

    def test_emails_are_normalised(self):
        rows, _ = MemberCsvParser().parse("email\n  Foo@Example.COM \n")
        assert rows[0].email == "foo@example.com"

    def test_missing_header_is_reported_once(self):
        rows, errors = MemberCsvParser().parse("name\nAnn\n")
        assert not rows
        assert len(errors) == 1
        assert errors[0].field == "header"

    def test_invalid_email_is_reported_with_its_row_number(self):
        rows, errors = MemberCsvParser().parse("email\na@x.com\nnot-an-email\n")
        assert len(rows) == 1
        assert len(errors) == 1
        # Row 1 is the header, so the bad row is row 3.
        assert errors[0].row == 3
        assert errors[0].field == "email"

    def test_duplicate_emails_are_reported(self):
        """PRD edge case: duplicate emails."""
        rows, errors = MemberCsvParser().parse("email\na@x.com\nA@x.com\n")
        assert len(rows) == 1
        assert any("Duplicate" in error.message for error in errors)

    def test_unknown_role_is_reported_with_the_allowed_values(self):
        _, errors = MemberCsvParser().parse("email,role\na@x.com,wizard\n")
        assert len(errors) == 1
        assert "participant" in errors[0].message

    def test_header_casing_and_spacing_are_tolerated(self):
        rows, errors = MemberCsvParser().parse(" Email , Role \na@x.com,admin\n")
        assert not errors
        assert rows[0].role is Role.ADMIN


class TestTeamCsv:
    def test_parses_teams_with_multiple_members(self):
        rows, errors = TeamCsvParser().parse(
            "team_name,project_title,emails\nRockets,Moon Lander,a@x.com;b@x.com\nComets,,c@x.com\n"
        )
        assert not errors
        assert rows[0].name == "Rockets"
        assert rows[0].project_title == "Moon Lander"
        assert rows[0].member_emails == ["a@x.com", "b@x.com"]
        assert rows[1].project_title is None

    def test_commas_work_as_a_separator_too(self):
        rows, errors = TeamCsvParser().parse('team_name,emails\nRockets,"a@x.com,b@x.com"\n')
        assert not errors
        assert rows[0].member_emails == ["a@x.com", "b@x.com"]

    def test_member_in_two_teams_is_reported(self):
        """PRD edge case: user belongs to multiple teams."""
        rows, errors = TeamCsvParser().parse("team_name,emails\nRockets,a@x.com\nComets,a@x.com\n")
        assert len(rows) == 1
        assert any("already assigned" in error.message for error in errors)

    def test_duplicate_team_names_are_reported(self):
        _, errors = TeamCsvParser().parse("team_name,emails\nRockets,a@x.com\nrockets,b@x.com\n")
        assert any("Duplicate team name" in error.message for error in errors)

    def test_team_with_no_valid_members_is_reported(self):
        rows, errors = TeamCsvParser().parse("team_name,emails\nRockets,\n")
        assert not rows
        assert errors[0].field == "emails"

    def test_blank_team_name_is_reported(self):
        rows, errors = TeamCsvParser().parse("team_name,emails\n,a@x.com\n")
        assert not rows
        assert errors[0].field == "team_name"

    def test_missing_required_columns_are_named(self):
        _, errors = TeamCsvParser().parse("team_name\nRockets\n")
        assert "emails" in errors[0].message
