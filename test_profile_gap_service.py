import unittest
from datetime import date

from services.profile_gap_service import profile_gap_report, profile_statuses, report_markdown, topic

PROFILE = """# ACME

## 1. Basic Information

| Item | Value |
| --- | --- |
| Company Name | ACME Ltd |
| Country of incorporation | India |

## 2. Annual Contractual Turnover

| Financial Year | Turnover |
| --- | --- |
| FY26 | 97100000000 |

## 11. Standing declarations and certificates

| Item | Value | Valid until |
| --- | --- | --- |
| Land border (GFR 144(xi)): bidder, or its owners, from a country sharing a land border with India |  |  |
| GST | 19AAAAA1234A1Z1 |  |
| GST Registration | Yes |  |

## Criteria marked by the bid team

| Criterion | Requirement | Status | TCNO | Marked on |
| --- | --- | --- | --- | --- |
| Not blacklisted | No debarment | Met | 1 | 2026-11-11 |
"""


def criterion(name, requirement='', status='unknown', **extra):
    return {'criterion': name, 'requirement': requirement, 'company': 'Not provided', 'status': status, **extra}


def result(tcno, criteria, processed_at='2026-11-05T10:00:00+00:00', status='completed'):
    return {'tcno': tcno, 'status': status, 'processed_at': processed_at, 'criteria': criteria}


class TopicTest(unittest.TestCase):
    def test_reasons_are_found_by_name_before_requirement(self):
        cases = {
            ('Land Border Registration', 'Bidders must be registered with the Competent Authority'): 'land_border',
            ('Local Content / MII', 'Class 1 local suppliers only'): 'make_in_india',
            # The requirement's MSE exemption is boilerplate: the name says what is missing
            ('Bidder Turnover', 'As in the bid document (exemption for MSE)'): 'financial',
            ('OEM Average Turnover', 'Required if the bidder is not the OEM'): 'oem',
            ('OEM Authorization (MAF)', 'A valid manufacturer authorisation'): 'oem_authorisation',
            ('PF, ESI & GST Registration', 'Copies of the registrations'): 'labour',
            ('Compliance', 'BIS Compliance is required'): 'iso_bis',
            ('Kolkata Office', 'An office with dedicated support staff'): 'local_office',
            ('Financial Standing', 'Not under liquidation or receivership'): 'integrity',
            ('Earnest Money Deposit', 'Rs 1,95,500'): 'fees',
            ('Minimum net-worth', 'Net-worth ≥ 10% of estimated cost'): 'financial',
            ('Estimated cost', 'Rs. 97.73 Lac'): 'tender_value',
            ('Site Inspection Letter', ''): 'bid_specific',
            ('Traditional Wagon Builder', 'Must not be a traditional wagon builder'): 'other',
        }
        for (name, requirement), key in cases.items():
            with self.subTest(name):
                self.assertEqual(topic(criterion(name, requirement))['key'], key)


class ProfileStatusTest(unittest.TestCase):
    def test_a_fact_is_stated_empty_or_missing_and_marks_by_hand_do_not_count(self):
        statuses = profile_statuses(PROFILE)
        self.assertEqual(statuses['gst_pan_udyam'], 'stated')
        self.assertEqual(statuses['financial'], 'stated')  # the heading names the table's rows
        self.assertEqual(statuses['land_border'], 'empty')
        self.assertEqual(statuses['make_in_india'], 'missing')
        # Only the marked criteria table speaks of blacklisting, and it is not a fact about the company
        self.assertEqual(statuses['integrity'], 'missing')
        # A GST registration is not a registration with the buyer
        self.assertEqual(statuses['registration'], 'missing')


class ReportTest(unittest.TestCase):
    def test_the_month_s_cannot_determine_criteria_are_counted_by_reason(self):
        results = [
            result('1', [criterion('Land Border Restriction'), criterion('Local Content'),
                         criterion('Turnover', status='met'),
                         # Marked Met by hand since: still a gap the model found
                         criterion('MII Compliance', status='met', ai_status='unknown', marked_by_user=True)]),
            result('2', [criterion('Land Border Eligibility'), criterion('Net worth', status='not_met')]),
            result('3', [criterion('Land Border')], processed_at='2026-10-30T23:00:00+00:00'),  # another month
            result('4', [criterion('Land Border')], status='failed'),
        ]
        report = profile_gap_report(results, PROFILE, '2026-11')

        self.assertEqual((report['label'], report['tenders'], report['criteria'], report['cannot_determine']),
                         ('November 2026', 2, 6, 4))
        self.assertEqual([(reason['key'], reason['criteria'], reason['tenders'], reason['share'], reason['profile'])
                          for reason in report['reasons']],
                         [('land_border', 2, 2, 50, 'empty'), ('make_in_india', 2, 1, 50, 'missing')])
        self.assertEqual(report['reasons'][1]['examples'], ['Local Content', 'MII Compliance'])

        markdown = report_markdown(report)
        self.assertIn('# Profile-gap report: November 2026', markdown)
        self.assertIn('| 1 | Land border declaration | 2 (50%) | 2 | Empty: fill it in |', markdown)
        self.assertIn('| 2 | Make in India class and local content | 2 (50%) | 1 | Not in the profile: add it |',
                      markdown)

    def test_the_current_month_is_the_default_and_an_empty_month_says_so(self):
        report = profile_gap_report([result('1', [criterion('Land Border')])], PROFILE, today=date(2026, 12, 2))
        self.assertEqual((report['month'], report['tenders'], report['reasons']), ('2026-12', 0, []))
        self.assertIn('No tender check was completed in December 2026.', report_markdown(report))


if __name__ == '__main__':
    unittest.main()
