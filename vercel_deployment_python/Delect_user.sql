-- check the user 
SELECT *
FROM public.audit_users
WHERE LOWER(TRIM(email)) = LOWER(TRIM('user@example.com'));   

-- delete query 
DELETE FROM public.audit_users
WHERE LOWER(TRIM(email)) = LOWER(TRIM('user@example.com'));

-- verify the user 
SELECT *
FROM public.audit_users
WHERE LOWER(TRIM(email)) = LOWER(TRIM('user@example.com'));