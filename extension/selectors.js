(() => {
  'use strict';
  globalThis.JobBotSelectors = Object.freeze({
    linkedin: Object.freeze({
      searchContainers: ['ul.jobs-search-results__list','div.jobs-search-results-list','[aria-label="Search results"]','[data-view-name="search-results-container"]'],
      resultCards: ['li.jobs-search-results__list-item','li[data-occludable-job-id]','.job-card-container','.base-card'],
      searchLinks: ['a[href*="/jobs/view/"]','a.job-card-list__title','a.job-card-container__link'],
      nextLinks: ['a[aria-label="View next page"][href]','a[aria-label*="Next"][href]','button[aria-label="View next page"] + a[href]'],
      nextButtons: ['button[aria-label="View next page"]:not([disabled])','button[aria-label*="Next"]:not([disabled])'],
      authPositive: ['a[href*="/my-items/saved-jobs"]','a[href*="/jobs/collections"]'],
      authSignIn: ['a[href*="/login"]','button[data-tracking-control-name*="signin"]','a[data-tracking-control-name*="signin"]'],
      title: ['h1.job-details-jobs-unified-top-card__job-title','h1[class*="job-title"]','h1','[data-test-job-title]'],
      company: ['.job-details-jobs-unified-top-card__company-name a','.job-details-jobs-unified-top-card__company-name','.topcard__org-name-link','a[href*="/company/"]'],
      location: ['.job-details-jobs-unified-top-card__primary-description-container','.job-details-jobs-unified-top-card__tertiary-description-container','.topcard__flavor--bullet','a[aria-label*="Remote"]'],
      description: ['.jobs-description-content__text','.jobs-description__content','#job-details','.show-more-less-html__markup','[class*="jobs-description"]'],
      posted: ['.job-details-jobs-unified-top-card__tertiary-description-container time','time'],
    }),
    indeed: Object.freeze({
      searchLinks: ['a[href*="/viewjob?jk="]','a[href*="/rc/clk?jk="]','h2.jobTitle a[href]','a[data-jk][href]','a[id^="job_"][href]'],
      nextLinks: ['a[data-testid="pagination-page-next"][href]','a[aria-label="Next Page"][href]','a[aria-label="Next"][href]','nav a[aria-label*="Next"][href]'],
      nextButtons: ['button[aria-label="Next Page"]:not([disabled])','button[aria-label="Next"]:not([disabled])'],
      authPositive: ['[data-testid*="myjobs"]','a[href*="/myjobs"]'],
      authSignIn: ['a[href*="account/login"]','a[href*="secure.indeed.com/auth"]'],
      title: ['h1[data-testid="jobsearch-JobInfoHeader-title"]','h1.jobsearch-JobInfoHeader-title','h1'],
      company: ['[data-testid="inlineHeader-companyName"]','[data-company-name="true"]','.jobsearch-InlineCompanyRating-companyHeader a','.jobsearch-InlineCompanyRating-companyHeader'],
      location: ['[data-testid="inlineHeader-companyLocation"]','[data-testid="job-location"]','.jobsearch-JobInfoHeader-subtitle div'],
      description: ['#jobDescriptionText','[data-testid="jobsearch-JobComponent-description"]','.jobsearch-JobComponent-description','main'],
      salary: ['#salaryInfoAndJobType','[data-testid="attribute_snippet_testid"]','[data-testid="salary-snippet-container"]'],
      posted: ['[data-testid="jobsearch-JobMetadataFooter"]','.jobsearch-JobMetadataFooter'],
    }),
    glassdoor: Object.freeze({
      searchLinks: ['a[href*="/job-listing/"]','a[data-test="job-link"][href]','a[class*="JobCard_jobTitle"][href]','a[class*="jobTitle"][href]'],
      nextLinks: ['a[data-test="pagination-next"][href]','a[aria-label="Next"][href]','a[aria-label="Next Page"][href]','nav a[href][aria-label*="Next"]'],
      authPositive: [],
      authSignIn: ['button[data-test="sign-in-button"]','a[href*="login"]','a[href*="sign-in"]'],
      title: ['h1[data-test="job-title"]','h1[class*="heading"]','h1'],
      company: ['[data-test="employer-name"]','[class*="EmployerProfile_employerName"]','[class*="employerName"]'],
      location: ['[data-test="location"]','[data-test="job-location"]','[class*="location"]'],
      description: ['[data-test="jobDescriptionContent"]','[class*="JobDetails_jobDescription"]','[class*="jobDescription"]','main'],
      salary: ['[data-test="detailSalary"]','[class*="salary"]'],
      posted: ['[data-test="job-age"]','[class*="listing-age"]','time'],
    }),
  });
})();
